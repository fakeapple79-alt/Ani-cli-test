from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup


class HindiProviderError(RuntimeError):
    """A safe, user-facing Hindi-provider failure."""


@dataclass(frozen=True)
class HindiAnimeResult:
    index: int
    provider: str
    provider_id: str
    title: str
    thumbnail: str | None = None


class HindiProvider:
    """Resolve Hindi-dubbed anime through one or more public provider adapters.

    The direct provider is bundled because it does not require a separate API
    deployment. Tatakai and AnimeWorld India are optional configured fallbacks;
    neither is called unless its base URL is supplied in the environment.
    """

    DIRECT_BASE_URL = "https://animehindidubbed.in"
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }

    def __init__(
        self,
        *,
        tatakai_base_url: str = "",
        animeworld_base_url: str = "",
        provider_order: tuple[str, ...] = ("direct", "tatakai", "animeworld"),
        timeout_seconds: float = 12.0,
    ) -> None:
        self.tatakai_base_url = tatakai_base_url.rstrip("/")
        self.animeworld_base_url = animeworld_base_url.rstrip("/")
        self.provider_order = tuple(name.strip().lower() for name in provider_order if name.strip())
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl_seconds = 120.0

    async def close(self) -> None:
        return None

    async def search(self, query: str, limit: int) -> list[HindiAnimeResult]:
        errors: list[str] = []
        for provider in self.provider_order:
            if provider == "tatakai" and not self.tatakai_base_url:
                continue
            if provider == "animeworld" and not self.animeworld_base_url:
                continue
            try:
                if provider == "direct":
                    results = await self._direct_search(query)
                elif provider == "tatakai":
                    results = await self._tatakai_search(query)
                elif provider == "animeworld":
                    results = await self._animeworld_search(query)
                else:
                    continue
                if results:
                    return [
                        HindiAnimeResult(
                            index=index,
                            provider=item.provider,
                            provider_id=item.provider_id,
                            title=item.title,
                            thumbnail=item.thumbnail,
                        )
                        for index, item in enumerate(results[:limit], start=1)
                    ]
            except HindiProviderError as exc:
                errors.append(f"{provider}: {exc}")

        if errors:
            raise HindiProviderError("Hindi-dub providers are temporarily unavailable.")
        raise HindiProviderError("No Hindi-dubbed anime matched that title.")

    async def get_episode_count(self, provider_id: str) -> int:
        provider, item_id = self._split_id(provider_id)
        if provider in {"direct", "tatakai"}:
            data = await self._get_provider_anime(provider, item_id)
            numbers = self._episode_numbers(data.get("episodes"))
            if not numbers:
                raise HindiProviderError("No Hindi episodes were found for that anime.")
            return max(numbers)

        if provider == "animeworld":
            seasons = await self._animeworld_seasons(item_id)
            if not seasons:
                raise HindiProviderError("No Hindi seasons were found for that anime.")
            episodes = await self._animeworld_episodes(str(seasons[0].get("seasonId", "")))
            numbers = [self._episode_number(item) for item in episodes]
            numbers = [number for number in numbers if number is not None]
            if not numbers:
                raise HindiProviderError("No Hindi episodes were found for that anime.")
            return max(numbers)

        raise HindiProviderError("That Hindi provider is not supported.")

    async def resolve_episode(self, provider_id: str, episode: int) -> str:
        provider, item_id = self._split_id(provider_id)
        if provider in {"direct", "tatakai"}:
            data = await self._get_provider_anime(provider, item_id)
            for item in data.get("episodes") or []:
                if self._episode_number(item) != episode:
                    continue
                servers = item.get("servers") if isinstance(item, dict) else None
                if not isinstance(servers, list):
                    continue
                for server in self._rank_servers(servers):
                    url = server.get("url") if isinstance(server, dict) else None
                    if isinstance(url, str) and url.startswith(("https://", "http://")):
                        return url
            raise HindiProviderError(f"No Hindi stream was found for episode {episode}.")

        if provider == "animeworld":
            seasons = await self._animeworld_seasons(item_id)
            if not seasons:
                raise HindiProviderError("No Hindi season was found for that anime.")
            season_id = str(seasons[0].get("seasonId", ""))
            episodes = await self._animeworld_episodes(season_id)
            target = next(
                (item for item in episodes if self._episode_number(item) == episode),
                None,
            )
            episode_id = target.get("episodeId") if isinstance(target, dict) else None
            if not isinstance(episode_id, str) or not episode_id:
                raise HindiProviderError(f"No Hindi stream was found for episode {episode}.")
            payload = await self._animeworld_json(
                "/api/anime-world-india/v1/stream.php",
                params={"episodeId": episode_id},
            )
            stream = payload.get("stream") if isinstance(payload, dict) else None
            if isinstance(stream, dict):
                for key in ("streamLink", "file"):
                    url = stream.get(key)
                    if isinstance(url, str) and url.startswith(("https://", "http://")):
                        return url
            raise HindiProviderError(f"No Hindi stream was found for episode {episode}.")

        raise HindiProviderError("That Hindi provider is not supported.")

    async def _get_provider_anime(self, provider: str, item_id: str) -> dict[str, Any]:
        if provider == "direct":
            return await self._direct_anime(item_id)
        return await self._tatakai_anime(item_id)

    async def _tatakai_search(self, query: str) -> list[HindiAnimeResult]:
        payload = await self._tatakai_json(
            f"/api/v2/anime/hindidubbed/search/{quote(query, safe='')}"
        )
        raw = payload.get("animeList") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise HindiProviderError("Tatakai returned an invalid search response.")
        results: list[HindiAnimeResult] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            title = item.get("title")
            if isinstance(slug, str) and isinstance(title, str) and title.strip():
                results.append(
                    HindiAnimeResult(
                        index=0,
                        provider="tatakai",
                        provider_id=f"tatakai:{slug}",
                        title=title.strip(),
                        thumbnail=item.get("thumbnail") if isinstance(item.get("thumbnail"), str) else None,
                    )
                )
        return results

    async def _tatakai_anime(self, slug: str) -> dict[str, Any]:
        payload = await self._tatakai_json(f"/api/v2/anime/hindidubbed/anime/{quote(slug, safe='')}")
        if not isinstance(payload, dict):
            raise HindiProviderError("Tatakai returned an invalid anime response.")
        return payload

    async def _direct_search(self, query: str) -> list[HindiAnimeResult]:
        cache_key = f"direct-search:{query.strip().lower()}"
        html = self._cache_get(cache_key)
        if not isinstance(html, str):
            html = await self._text(
                f"{self.DIRECT_BASE_URL}/?s={quote(query, safe='')}",
                referer=self.DIRECT_BASE_URL,
            )
            self._cache_put(cache_key, html)
        soup = BeautifulSoup(html, "html.parser")
        results: list[HindiAnimeResult] = []
        seen: set[str] = set()
        for item in soup.select("article, .post, .type-post"):
            title_node = item.select_one(".entry-title a, .post-title a, h2 a")
            if title_node is None:
                continue
            title = title_node.get_text(" ", strip=True)
            href = title_node.get("href") or ""
            match = re.search(r"animehindidubbed\.in/([^/?#]+)", href)
            if not title or not match:
                continue
            slug = match.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            image = item.select_one("img")
            thumbnail = image.get("src") if image is not None else None
            results.append(
                HindiAnimeResult(
                    index=0,
                    provider="direct",
                    provider_id=f"direct:{slug}",
                    title=title,
                    thumbnail=thumbnail if isinstance(thumbnail, str) else None,
                )
            )
        return results

    async def _direct_anime(self, slug: str) -> dict[str, Any]:
        cache_key = f"direct-anime:{slug}"
        cached = self._cache_get(cache_key)
        if isinstance(cached, dict):
            return cached
        html = await self._text(f"{self.DIRECT_BASE_URL}/{quote(slug, safe='')}/", referer=self.DIRECT_BASE_URL)
        soup = BeautifulSoup(html, "html.parser")
        title = soup.select_one("article h1.entry-title, .entry-header h1, h1.entry-title")
        episodes: dict[int, dict[str, Any]] = {}
        script = "\n".join(node.get_text() for node in soup.find_all("script"))
        match = re.search(r"const\s+serverVideos\s*=\s*({[\s\S]*?})\s*;", script)
        if match:
            data = self._parse_js_object(match.group(1))
            for server_name, items in data.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "")
                    number_match = re.search(r"S\d+E(\d+)", name, flags=re.IGNORECASE)
                    if number_match is None:
                        number_match = re.search(r"(\d+)", name)
                    if number_match is None:
                        continue
                    number = int(number_match.group(1))
                    url = item.get("url")
                    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                        continue
                    episodes.setdefault(number, {"number": number, "servers": []})["servers"].append(
                        {"name": server_name, "url": url, "language": "Hindi"}
                    )
        result = {
            "title": title.get_text(" ", strip=True) if title else slug.replace("-", " "),
            "episodes": [episodes[number] for number in sorted(episodes)],
        }
        self._cache_put(cache_key, result)
        return result

    def _cache_get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        return value

    def _cache_put(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic() + self._cache_ttl_seconds, value)

    async def _animeworld_search(self, query: str) -> list[HindiAnimeResult]:
        payload = await self._animeworld_json(
            "/api/anime-world-india/v1/search.php",
            params={"query": query, "p": 1},
        )
        raw = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise HindiProviderError("AnimeWorld India returned an invalid search response.")
        results: list[HindiAnimeResult] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("type") == "movie":
                continue
            series_id = item.get("seriesId")
            title = item.get("title")
            if isinstance(series_id, str) and isinstance(title, str) and title.strip():
                results.append(
                    HindiAnimeResult(
                        index=0,
                        provider="animeworld",
                        provider_id=f"animeworld:{series_id}",
                        title=title.strip(),
                        thumbnail=item.get("image") if isinstance(item.get("image"), str) else None,
                    )
                )
        return results

    async def _animeworld_seasons(self, series_id: str) -> list[dict[str, Any]]:
        payload = await self._animeworld_json(
            "/api/anime-world-india/v1/seasons.php",
            params={"seriesID": series_id},
        )
        seasons = payload.get("seasons") if isinstance(payload, dict) else None
        return seasons if isinstance(seasons, list) else []

    async def _animeworld_episodes(self, season_id: str) -> list[dict[str, Any]]:
        payload = await self._animeworld_json(
            "/api/anime-world-india/v1/episodes.php",
            params={"seasonId": season_id},
        )
        episodes = payload.get("episodes") if isinstance(payload, dict) else None
        return episodes if isinstance(episodes, list) else []

    async def _tatakai_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._json(urljoin(f"{self.tatakai_base_url}/", path.lstrip("/")), params)

    async def _animeworld_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._json(urljoin(f"{self.animeworld_base_url}/", path.lstrip("/")), params)

    async def _json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=self.DEFAULT_HEADERS) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HindiProviderError("The Hindi provider returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise HindiProviderError("The Hindi provider returned an invalid response.")
        if payload.get("success") is False and payload.get("error"):
            raise HindiProviderError(str(payload["error"]))
        return payload

    async def _text(self, url: str, referer: str) -> str:
        try:
            headers = {**self.DEFAULT_HEADERS, "Referer": referer}
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
                response = await client.get(url)
                response.raise_for_status()
                body = response.text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                return await asyncio.to_thread(self._curl_text, url)
            raise HindiProviderError("The Hindi provider is temporarily unavailable.") from exc
        except httpx.HTTPError as exc:
            raise HindiProviderError("The Hindi provider is temporarily unavailable.") from exc
        if not body or "Just a moment..." in body:
            raise HindiProviderError("The Hindi provider is temporarily unavailable.")
        return body

    @classmethod
    def _curl_text(cls, url: str) -> str:
        curl = shutil.which("curl")
        if not curl:
            raise HindiProviderError("The Hindi provider is temporarily unavailable.")
        command = [
            curl,
            "-sS",
            "-L",
            "--max-time",
            "15",
            url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HindiProviderError("The Hindi provider is temporarily unavailable.") from exc
        body = completed.stdout or ""
        if completed.returncode != 0 or not body or "Just a moment..." in body:
            raise HindiProviderError("The Hindi provider is temporarily unavailable.")
        return body

    @staticmethod
    def _parse_js_object(source: str) -> dict[str, Any]:
        normalized = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', source)
        normalized = re.sub(r",\s*([}\]])", r"\1", normalized)
        normalized = normalized.replace("'", '"')
        try:
            import json

            value = json.loads(normalized)
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _episode_number(item: Any) -> int | None:
        if not isinstance(item, dict):
            return None
        value = item.get("number", item.get("episodeNumber", item.get("episode")))
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str):
            match = re.search(r"\d+", value)
            if match:
                number = int(match.group(0))
                return number if number > 0 else None
        return None

    def _episode_numbers(self, items: Any) -> list[int]:
        if not isinstance(items, list):
            return []
        return [number for item in items if (number := self._episode_number(item)) is not None]

    @staticmethod
    def _rank_servers(items: list[Any]) -> list[dict[str, Any]]:
        ranked: list[tuple[int, dict[str, Any]]] = []
        preference = {"filemoon": 0, "vidgroud": 1, "stream": 2}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").lower()
            if "servabyss" in name or "abyss" in name:
                continue
            ranked.append((min((rank for key, rank in preference.items() if key in name), default=9), item))
        ranked.sort(key=lambda pair: pair[0])
        return [item for _, item in ranked]

    @staticmethod
    def _split_id(provider_id: str) -> tuple[str, str]:
        provider, separator, item_id = str(provider_id).partition(":")
        if not separator or provider not in {"direct", "tatakai", "animeworld"} or not item_id:
            raise HindiProviderError("That Hindi selection is no longer valid. Search again.")
        return provider, item_id


__all__ = ["HindiAnimeResult", "HindiProvider", "HindiProviderError"]
