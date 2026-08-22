from __future__ import annotations

import asyncio
import base64
import json
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
    DESIDUBANIME_BASE_URL = "https://www.desidubanime.me"
    ANIMESKY_BASE_URL = "https://animesky.top"
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
        animesky_base_url: str = ANIMESKY_BASE_URL,
        desidubanime_base_url: str = DESIDUBANIME_BASE_URL,
        tatakai_base_url: str = "",
        animeworld_base_url: str = "",
        provider_order: tuple[str, ...] = ("desidubanime", "animesky", "direct", "tatakai", "animeworld"),
        timeout_seconds: float = 12.0,
    ) -> None:
        self.animesky_base_url = animesky_base_url.rstrip("/") or self.ANIMESKY_BASE_URL
        self.desidubanime_base_url = desidubanime_base_url.rstrip("/") or self.DESIDUBANIME_BASE_URL
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
            if provider == "desidubanime" and not self.desidubanime_base_url:
                continue
            if provider == "animesky" and not self.animesky_base_url:
                continue
            if provider == "tatakai" and not self.tatakai_base_url:
                continue
            if provider == "animeworld" and not self.animeworld_base_url:
                continue
            try:
                if provider == "desidubanime":
                    results = await self._desidubanime_search(query)
                elif provider == "animesky":
                    results = await self._animesky_search(query)
                elif provider == "direct":
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
        if provider == "desidubanime":
            catalog = await self._desidubanime_catalog(item_id)
            numbers = self._episode_numbers(catalog)
            if not numbers:
                raise HindiProviderError("No Hindi episodes were found for that anime.")
            return max(numbers)

        if provider == "animesky":
            catalog = await self._animesky_catalog(item_id)
            numbers = [self._episode_number(item) for item in catalog]
            numbers = [number for number in numbers if number is not None]
            if not numbers:
                raise HindiProviderError("No Hindi episodes were found for that anime.")
            return max(numbers)

        if provider in {"direct", "tatakai"}:
            data = await self._get_provider_anime(provider, item_id)
            numbers = self._episode_numbers(data.get("episodes"))
            if not numbers:
                raise HindiProviderError("No Hindi episodes were found for that anime.")
            return max(numbers)

        if provider == "animeworld":
            episodes = await self._animeworld_catalog(item_id)
            numbers = [self._episode_number(item) for item in episodes]
            numbers = [number for number in numbers if number is not None]
            if not numbers:
                raise HindiProviderError("No Hindi episodes were found for that anime.")
            return max(numbers)

        raise HindiProviderError("That Hindi provider is not supported.")

    async def resolve_episode(self, provider_id: str, episode: int) -> str:
        provider, item_id = self._split_id(provider_id)
        if provider == "desidubanime":
            catalog = await self._desidubanime_catalog(item_id)
            target = next(
                (item for item in catalog if self._episode_number(item) == episode),
                None,
            )
            watch_slug = target.get("watch_slug") if isinstance(target, dict) else None
            if not isinstance(watch_slug, str) or not watch_slug:
                raise HindiProviderError(f"No Hindi stream was found for episode {episode}.")
            servers = await self._desidubanime_watch_servers(watch_slug)
            for server in self._rank_desidubanime_servers(servers):
                url = server.get("url") if isinstance(server, dict) else None
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    return url
            raise HindiProviderError(f"No Hindi stream was found for episode {episode}.")

        if provider == "animesky":
            catalog = await self._animesky_catalog(item_id)
            target = next(
                (item for item in catalog if self._episode_number(item) == episode),
                None,
            )
            url = target.get("url") if isinstance(target, dict) else None
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                return url
            raise HindiProviderError(f"No Hindi stream was found for episode {episode}.")

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
            episodes = await self._animeworld_catalog(item_id)
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

    async def _desidubanime_search(self, query: str) -> list[HindiAnimeResult]:
        cache_key = f"desidubanime-search:{query.strip().lower()}"
        cached = self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached

        payload = await self._desidubanime_advanced_search(query)
        data = payload.get("data") if isinstance(payload, dict) else None
        html = data.get("html") if isinstance(data, dict) else None
        if not isinstance(html, str) or not html.strip():
            return []

        soup = BeautifulSoup(html, "html.parser")
        results: list[HindiAnimeResult] = []
        seen: set[str] = set()
        for card in soup.select(".anime-card"):
            spans = [node.get_text(" ", strip=True) for node in card.select("h3 a span")]
            title = next((value for value in reversed(spans) if value), "")
            image = card.select_one("img")
            if not title and image is not None:
                title = str(image.get("alt") or "").strip()

            detail_url = ""
            info_button = card.select_one("button[onclick*='/anime/']")
            if info_button is not None:
                onclick = str(info_button.get("onclick") or "")
                match = re.search(r"window\.location\.href=['\"]([^'\"]+)['\"]", onclick)
                if match:
                    detail_url = match.group(1)
            if not detail_url:
                anchor = card.select_one("h3 a.stretched-link, a.stretched-link, a[href*='/anime/']")
                if anchor is not None:
                    detail_url = str(anchor.get("href") or "")

            match = re.search(r"/anime/([^/?#]+)", detail_url)
            if not match:
                continue
            slug = match.group(1).strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            thumbnail = None
            if image is not None:
                candidate = image.get("data-src") or image.get("src")
                if isinstance(candidate, str) and candidate.strip():
                    thumbnail = candidate.strip()
            results.append(
                HindiAnimeResult(
                    index=0,
                    provider="desidubanime",
                    provider_id=f"desidubanime:{slug}",
                    title=title or slug.replace("-", " "),
                    thumbnail=thumbnail,
                )
            )
        self._cache_put(cache_key, results)
        return results

    async def _desidubanime_advanced_search(self, query: str) -> dict[str, Any]:
        url = f"{self.desidubanime_base_url}/wp-admin/admin-ajax.php"
        form = {
            "action": "advanced_search",
            "page": "1",
            "s_keyword": query,
            "orderby": "date",
            "order": "DESC",
        }
        return await self._desidubanime_request(url, method="POST", data=form, referer=f"{self.desidubanime_base_url}/search/")

    async def _desidubanime_details(self, slug: str) -> dict[str, Any]:
        cache_key = f"desidubanime-details:{slug}"
        cached = self._cache_get(cache_key)
        if isinstance(cached, dict):
            return cached
        html = await self._text(
            f"{self.desidubanime_base_url}/anime/{quote(slug, safe='')}/",
            referer=self.desidubanime_base_url,
        )
        soup = BeautifulSoup(html, "html.parser")
        post_id = None
        post_input = soup.select_one("input#comment_post_ID")
        if post_input is not None:
            post_id = str(post_input.get("value") or "").strip() or None
        if not post_id:
            match = re.search(r"showWatchlistModal\('#watchlist-(\d+)'\)", html)
            post_id = match.group(1) if match else None
        if not post_id:
            match = re.search(r'"postId"\s*:\s*"(\d+)"', html)
            post_id = match.group(1) if match else None

        seasons: list[dict[str, str]] = []
        for button in soup.select("#seasonButtonsContainer button[data-season]"):
            season_id = str(button.get("data-season") or "").strip()
            if season_id:
                seasons.append(
                    {
                        "season_id": season_id,
                        "season_name": button.get_text(" ", strip=True) or "Season",
                    }
                )
        if not seasons and post_id:
            seasons.append({"season_id": post_id, "season_name": "Season 1"})
        result = {"slug": slug, "post_id": post_id, "seasons": seasons}
        self._cache_put(cache_key, result)
        return result

    @staticmethod
    def _desidubanime_select_seasons(slug: str, seasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def normalize(value: str) -> str:
            normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
            return normalized.replace("shippuuden", "shippuden")

        normalized_slug = normalize(slug)
        exact = [
            season
            for season in seasons
            if normalize(str(season.get("season_name") or "")) == normalized_slug
        ]
        if exact:
            return exact

        slug_terms = set(normalized_slug.split())
        matches = [
            season
            for season in seasons
            if slug_terms
            and slug_terms.issubset(
                set(normalize(str(season.get("season_name") or "")).split())
            )
        ]
        return matches or seasons

    async def _desidubanime_catalog(self, slug: str) -> list[dict[str, Any]]:
        cache_key = f"desidubanime-catalog:{slug}"
        cached = self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached
        details = await self._desidubanime_details(slug)
        seasons = details.get("seasons") if isinstance(details, dict) else None
        if not isinstance(seasons, list) or not seasons:
            raise HindiProviderError("No Hindi seasons were found for that anime.")
        seasons = self._desidubanime_select_seasons(slug, seasons)

        catalog: list[dict[str, Any]] = []
        seen_watch_slugs: set[str] = set()
        for season_index, season in enumerate(seasons, start=1):
            if not isinstance(season, dict):
                continue
            season_id = str(season.get("season_id") or "").strip()
            if not season_id:
                continue
            page = 1
            max_pages = 1
            while page <= max_pages and page <= 100:
                batch_end = min(max_pages, page + 7, 100)
                page_numbers = list(range(page, batch_end + 1))
                requests = [
                    self._desidubanime_request(
                        f"{self.desidubanime_base_url}/wp-admin/admin-ajax.php",
                        method="GET",
                        params={
                            "action": "get_episodes",
                            "anime_id": season_id,
                            "page": str(page_number),
                            "order": "asc",
                        },
                        referer=f"{self.desidubanime_base_url}/anime/{quote(slug, safe='')}/",
                    )
                    for page_number in page_numbers
                ]
                payloads = await asyncio.gather(*requests, return_exceptions=True)
                progressed = False
                for payload in payloads:
                    if not isinstance(payload, dict):
                        continue
                    data = payload.get("data")
                    raw_episodes = data.get("episodes") if isinstance(data, dict) else None
                    if not isinstance(raw_episodes, list) or not raw_episodes:
                        continue
                    progressed = True
                    try:
                        max_pages = max(max_pages, int(data.get("max_episodes_page") or 1))
                    except (TypeError, ValueError):
                        pass
                    for raw in raw_episodes:
                        if not isinstance(raw, dict):
                            continue
                        watch_url = str(raw.get("url") or "")
                        match = re.search(r"/watch/([^/?#]+)/?", watch_url)
                        watch_slug = match.group(1) if match else ""
                        if not watch_slug or watch_slug in seen_watch_slugs:
                            continue
                        source_number = self._episode_number(raw)
                        if source_number is None:
                            number_match = re.search(r"-episode-(\d+)", watch_slug, re.IGNORECASE)
                            source_number = int(number_match.group(1)) if number_match else None
                        if source_number is None:
                            continue
                        seen_watch_slugs.add(watch_slug)
                        catalog.append(
                            {
                                "number": len(catalog) + 1,
                                "season": season_index,
                                "episode": source_number,
                                "watch_slug": watch_slug,
                                "title": str(raw.get("title") or f"Episode {source_number}"),
                                "url": watch_url,
                            }
                        )
                if not progressed:
                    break
                page = batch_end + 1
        if not catalog:
            raise HindiProviderError("No Hindi episodes were found for that anime.")
        self._cache_put(cache_key, catalog)
        return catalog

    async def _desidubanime_watch_servers(self, watch_slug: str) -> list[dict[str, Any]]:
        cache_key = f"desidubanime-watch:{watch_slug}"
        cached = self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached
        html = await self._text(
            f"{self.desidubanime_base_url}/watch/{quote(watch_slug, safe='')}/",
            referer=self.desidubanime_base_url,
        )
        soup = BeautifulSoup(html, "html.parser")
        servers: list[dict[str, Any]] = []
        for element in soup.select("[data-embed-id]"):
            decoded = self._decode_desidubanime_embed(str(element.get("data-embed-id") or ""))
            if decoded is None:
                continue
            if not any(item.get("url") == decoded["url"] for item in servers):
                servers.append({"name": decoded["name"], "url": decoded["url"], "language": "Hindi"})
        # GDMirror is a directory entry. Expand it through the same helper
        # used by the upstream DesiDubAnime API so StreamHG/Hanerix and other
        # current mirrors are available when the site’s wrapper players fail.
        for server in list(servers):
            server_url = str(server.get("url") or "")
            if "gdmirrorbot.nl" not in server_url.lower():
                continue
            for mirror in await self._desidubanime_expand_gdmirror(server_url):
                if not any(item.get("url") == mirror.get("url") for item in servers):
                    servers.append(mirror)

        if not servers:
            raise HindiProviderError("No Hindi stream servers were found for that episode.")
        self._cache_put(cache_key, servers)
        return servers

    async def _desidubanime_expand_gdmirror(self, embed_url: str) -> list[dict[str, Any]]:
        match = re.search(r"gdmirrorbot\.nl/(?:embed/)?([^/?#]+)", embed_url, re.IGNORECASE)
        if not match:
            return []
        sid = match.group(1).strip()
        if not sid:
            return []
        payload = {
            "sid": sid,
            "UserFavSite": "",
            "currentDomain": self.desidubanime_base_url,
        }
        headers = {
            **self.DEFAULT_HEADERS,
            "Referer": "https://pro.iqsmartgames.com/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.post(
                    "https://pro.iqsmartgames.com/embedhelper.php",
                    data=payload,
                )
                response.raise_for_status()
                helper_payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        return self._decode_desidubanime_helper_servers(helper_payload)

    @staticmethod
    def _decode_desidubanime_helper_servers(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        encoded = payload.get("mresult")
        site_urls = payload.get("siteUrls")
        friendly_names = payload.get("siteFriendlyNames")
        if not isinstance(encoded, str) or not isinstance(site_urls, dict):
            return []
        try:
            padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
            decoded = json.loads(base64.b64decode(padded).decode("utf-8"))
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            return []
        if not isinstance(decoded, dict):
            return []

        servers: list[dict[str, Any]] = []
        for key, code in decoded.items():
            base_url = site_urls.get(key)
            if not isinstance(base_url, str) or not isinstance(code, str):
                continue
            url = f"{base_url}{code}"
            if not url.startswith(("https://", "http://")):
                continue
            name = friendly_names.get(key, key) if isinstance(friendly_names, dict) else key
            servers.append({
                "name": str(name or key),
                "url": url,
                "language": "Hindi",
            })
        return servers

    async def _desidubanime_request(
        self,
        url: str,
        *,
        method: str,
        referer: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {**self.DEFAULT_HEADERS, "Referer": referer}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
                if method.upper() == "POST":
                    response = await client.post(url, data=data or {})
                else:
                    response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HindiProviderError("DesiDubAnime returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise HindiProviderError("DesiDubAnime returned an invalid response.")
        return payload

    @staticmethod
    def _decode_desidubanime_embed(embed_id: str) -> dict[str, str] | None:
        if not embed_id or ":" not in embed_id:
            return None
        try:
            name_encoded, url_encoded = embed_id.split(":", 1)
            name = base64.b64decode(name_encoded + "=" * ((4 - len(name_encoded) % 4) % 4)).decode("utf-8", "ignore").strip()
            url = base64.b64decode(url_encoded + "=" * ((4 - len(url_encoded) % 4) % 4)).decode("utf-8", "ignore").strip()
            if "<iframe" in url.lower():
                iframe_match = re.search(r"src=['\"]([^'\"]+)", url, re.IGNORECASE)
                if iframe_match:
                    url = iframe_match.group(1).strip()
            if not name or not url or not url.startswith(("https://", "http://")):
                return None
            return {"name": name, "url": url}
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _rank_desidubanime_servers(items: list[Any]) -> list[dict[str, Any]]:
        # Expanded StreamHG/Hanerix links are direct browser player pages and
        # have proven more reliable than the site's Cloud wrapper and stale
        # Abyss identifiers. Keep the original mirrors as fallbacks.
        preference = {
            "streamhg": 0,
            "hanerix": 0,
            "cloud": 1,
            "abyss": 2,
            "mirror": 3,
            "playerx": 4,
            "gdmirror": 5,
        }
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").lower()
            rank = min((value for key, value in preference.items() if key in name), default=9)
            ranked.append((rank, index, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1]))
        return [item for _, _, item in ranked]

    async def _animesky_search(self, query: str) -> list[HindiAnimeResult]:
        cache_key = f"animesky-search:{query.strip().lower()}"
        html = self._cache_get(cache_key)
        if not isinstance(html, str):
            html = await self._text(
                f"{self.animesky_base_url}/?s={quote(query, safe='')}",
                referer=self.animesky_base_url,
            )
            self._cache_put(cache_key, html)

        soup = BeautifulSoup(html, "html.parser")
        results: list[HindiAnimeResult] = []
        seen: set[str] = set()
        for anchor in soup.select('a[href*="/series/"]'):
            href = urljoin(f"{self.animesky_base_url}/", str(anchor.get("href") or ""))
            match = re.search(r"/series/([^/?#]+)/?", href)
            if not match:
                continue
            slug = match.group(1).strip()
            if not slug or slug in seen:
                continue
            title_node = anchor.select_one("h1, h2, h3, h4, .entry-title, .post-title, .title")
            image = anchor.find("img")
            title = (
                title_node.get_text(" ", strip=True) if title_node is not None else ""
            ) or (image.get("alt", "") if image is not None else "") or anchor.get_text(" ", strip=True)
            title = re.sub(r"\s+", " ", title).strip()
            if not title or title.lower() in {"watch now", "read more", "play"}:
                continue
            seen.add(slug)
            results.append(
                HindiAnimeResult(
                    index=0,
                    provider="animesky",
                    provider_id=f"animesky:{slug}",
                    title=title,
                    thumbnail=image.get("src") if image is not None and isinstance(image.get("src"), str) else None,
                )
            )
        return results

    async def _animesky_catalog(self, slug: str) -> list[dict[str, Any]]:
        cache_key = f"animesky-catalog:{slug}"
        cached = self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached

        html = await self._text(
            f"{self.animesky_base_url}/series/{quote(slug, safe='')}/",
            referer=self.animesky_base_url,
        )
        soup = BeautifulSoup(html, "html.parser")
        season_ranges: list[tuple[int, int, int]] = []
        for button in soup.select("a.season-btn[data-season]"):
            classes = {str(value).strip().lower() for value in (button.get("class") or [])}
            if "non-regional" in classes:
                continue
            season_match = re.search(r"\d+", str(button.get("data-season") or ""))
            range_node = button.select_one(".season-episodes")
            if not season_match or range_node is None:
                continue
            numbers = [
                int(value)
                for value in re.findall(r"\d+", range_node.get_text(" ", strip=True))
            ]
            if len(numbers) < 2:
                continue
            season_ranges.append((int(season_match.group(0)), numbers[0], numbers[1]))

        catalog: list[dict[str, Any]] = []
        local_number = 0
        for season, start, end in season_ranges:
            for season_episode in range(start, end + 1):
                local_number += 1
                catalog.append(
                    {
                        "number": local_number,
                        "season": season,
                        "episode": season_episode,
                        "url": f"{self.animesky_base_url}/episode/{quote(slug, safe='')}-{season}x{season_episode}/",
                    }
                )
        if not catalog:
            raise HindiProviderError("No Hindi episodes were found for that anime.")
        self._cache_put(cache_key, catalog)
        return catalog

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
        html = await self._text(
            f"{self.DIRECT_BASE_URL}/{quote(slug, safe='')}/",
            referer=self.DIRECT_BASE_URL,
        )
        soup = BeautifulSoup(html, "html.parser")
        title = soup.select_one("article h1.entry-title, .entry-header h1, h1.entry-title")
        episode_buckets: dict[tuple[int, int], dict[str, Any]] = {}
        script = "\n".join(node.get_text() for node in soup.find_all("script"))
        match = re.search(r"const\s+serverVideos\s*=\s*({[\s\S]*?})\s*;", script)
        if match:
            data = self._parse_js_object(match.group(1))
            accepted_servers: list[tuple[str, list[Any]]] = []
            for server_name, items in data.items():
                # The page may include explicit English-only or Servabyss
                # lists alongside Hindi/multi-audio servers. English must not
                # enter Hindi mode, and Servabyss is already excluded by the
                # stream ranking logic; its plain 01..N labels would otherwise
                # create fake Season 1 episodes for a multi-season catalog.
                server_key = str(server_name).strip().lower()
                if server_key in {"english", "englishdub", "english-dub"}:
                    continue
                if "abyss" in server_key or not isinstance(items, list):
                    continue
                accepted_servers.append((str(server_name), items))

            structured_groups: list[tuple[str, list[tuple[dict[str, Any], tuple[int, int]]]]] = []
            for server_name, items in accepted_servers:
                parsed_items: list[tuple[dict[str, Any], tuple[int, int]]] = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    season_episode = self._direct_season_episode(
                        str(item.get("name") or ""),
                        str(item.get("url") or ""),
                    )
                    if season_episode is not None:
                        parsed_items.append((item, season_episode))
                # Treat a server as season-structured only when most of its
                # rows carry season metadata. This prevents four incidental
                # S6E06-style URL markers from corrupting a 360-row numeric
                # catalog such as Naruto Shippuden.
                if parsed_items and len(parsed_items) * 2 >= len(items):
                    structured_groups.append((server_name, parsed_items))

            if structured_groups:
                for server_name, parsed_items in structured_groups:
                    for item, (season, source_episode) in parsed_items:
                        url = item.get("url")
                        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                            continue
                        key = (season, source_episode)
                        episode_buckets.setdefault(
                            key,
                            {
                                "season": season,
                                "source_episode": source_episode,
                                "servers": [],
                            },
                        )["servers"].append(
                            {"name": server_name, "url": url, "language": "Hindi"}
                        )
            else:
                # Numeric catalogs with no reliable season metadata use the
                # provider's displayed/list order as one continuous range.
                for server_name, items in accepted_servers:
                    for index, item in enumerate(items, start=1):
                        if not isinstance(item, dict):
                            continue
                        url = item.get("url")
                        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                            continue
                        key = (1, index)
                        episode_buckets.setdefault(
                            key,
                            {
                                "season": 1,
                                "source_episode": index,
                                "servers": [],
                            },
                        )["servers"].append(
                            {"name": server_name, "url": url, "language": "Hindi"}
                        )

        episodes: list[dict[str, Any]] = []
        for local_number, key in enumerate(sorted(episode_buckets), start=1):
            item = episode_buckets[key]
            episodes.append(
                {
                    "number": local_number,
                    "season": item["season"],
                    "episode": item["source_episode"],
                    "servers": item["servers"],
                }
            )
        result = {
            "title": title.get_text(" ", strip=True) if title else slug.replace("-", " "),
            "episodes": episodes,
        }
        self._cache_put(cache_key, result)
        return result

    @staticmethod
    def _direct_season_episode(name: str, url: str = "") -> tuple[int, int] | None:
        # Some pages label every row only as `01` or `Episode 1`; their URL
        # carries the real season marker, such as `...S02E01...`.
        normalized = re.sub(r"[_-]+", " ", name).strip()
        source = f"{normalized} {url}"
        match = re.search(
            r"s(?:eason)?\s*(\d+)\s*e(?:p|pisode)?\s*0*(\d+)",
            source,
            flags=re.IGNORECASE,
        )
        if match:
            return int(match.group(1)), int(match.group(2))
        match = re.search(
            r"season\s*0*(\d+).*?episode\s*0*(\d+)",
            source,
            flags=re.IGNORECASE,
        )
        if match:
            return int(match.group(1)), int(match.group(2))
        return None

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

    async def _animeworld_catalog(self, series_id: str) -> list[dict[str, Any]]:
        cache_key = f"animeworld-catalog:{series_id}"
        cached = self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached

        seasons = await self._animeworld_seasons(series_id)
        if not seasons:
            raise HindiProviderError("No Hindi seasons were found for that anime.")

        def season_order(item: Any) -> int:
            if not isinstance(item, dict):
                return 10**9
            match = re.search(r"\d+", str(item.get("seasonNumber") or ""))
            return int(match.group(0)) if match else 10**9

        ordered_seasons = sorted(enumerate(seasons), key=lambda pair: (season_order(pair[1]), pair[0]))
        catalog: list[dict[str, Any]] = []
        local_number = 0
        for season_index, season in ordered_seasons:
            if not isinstance(season, dict):
                continue
            season_id = str(season.get("seasonId") or "").strip()
            if not season_id:
                continue
            for item in await self._animeworld_episodes(season_id):
                source_number = self._episode_number(item)
                if source_number is None or not isinstance(item, dict):
                    continue
                local_number += 1
                normalized = dict(item)
                normalized["number"] = local_number
                normalized["_season_number"] = season.get("seasonNumber") or season_index + 1
                normalized["_season_episode"] = source_number
                catalog.append(normalized)

        self._cache_put(cache_key, catalog)
        return catalog

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
        try:
            import ast
            import json

            try:
                value = json.loads(normalized)
            except (ValueError, TypeError):
                # Provider URLs occasionally contain apostrophes. Do not
                # globally replace apostrophes because they may be inside a
                # double-quoted URL; Python's literal parser handles both
                # quote styles safely after keys are quoted above.
                value = ast.literal_eval(normalized)
        except (ValueError, TypeError, SyntaxError):
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
        if not separator or provider not in {"desidubanime", "animesky", "direct", "tatakai", "animeworld"} or not item_id:
            raise HindiProviderError("That Hindi selection is no longer valid. Search again.")
        return provider, item_id


__all__ = ["HindiAnimeResult", "HindiProvider", "HindiProviderError"]
