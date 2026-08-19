from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from .config import (
    ANIDB_BASE_URL,
    ANIDB_USER_AGENT,
    MAX_EPISODE,
    MAX_QUERY_LENGTH,
    MAX_RESULT_INDEX,
    MAX_SEARCH_RESULTS,
    settings,
)


class ResolverError(RuntimeError):
    """A safe, user-facing resolver failure."""


@dataclass(frozen=True)
class AnimeResult:
    index: int
    anime_id: str
    title: str


class AniCliResolver:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_resolvers)

    async def close(self) -> None:
        """Kept for graceful shutdown compatibility; curl uses short-lived processes."""
        return None

    async def search(self, query: str) -> list[AnimeResult]:
        query = query.strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
            raise ResolverError("Please send a shorter anime title.")

        try:
            html = await asyncio.to_thread(self._fetch_search_page, query)
        except ResolverError:
            raise
        except Exception as exc:
            raise ResolverError(
                "The anime search provider is temporarily unavailable."
            ) from exc

        results: list[AnimeResult] = []
        seen: set[str] = set()
        soup = BeautifulSoup(html, "html.parser")

        # This mirrors the current ani-cli search contract: /anime/<slug-id>
        # with the display title in the anchor/card markup.
        for anchor in soup.select('a[href*="/anime/"]'):
            href = anchor.get("href", "")
            match = re.search(r"/anime/([^/?#]+)", href)
            if not match:
                continue

            anime_id = match.group(1)
            if anime_id in seen or not re.search(r"-\d+$", anime_id):
                continue

            image = anchor.find("img")
            title = (
                anchor.get("alt")
                or anchor.get("title")
                or (image.get("alt") if image else None)
                or anchor.get_text(" ", strip=True)
            )
            title = title.strip() if title else ""
            if not title:
                continue

            seen.add(anime_id)
            results.append(
                AnimeResult(index=len(results) + 1, anime_id=anime_id, title=title)
            )
            if len(results) >= MAX_SEARCH_RESULTS:
                break

        if not results:
            raise ResolverError("No anime results found. Try another title.")
        return results

    @staticmethod
    def _curl_executable() -> str:
        # Current ani-cli checks these browser-impersonating names first.
        for candidate in (
            "curl_firefox135",
            "curl_chrome136",
            "curl_chrome116",
            "curl_ff117",
            "curl",
        ):
            path = shutil.which(candidate)
            if path:
                return path
        raise ResolverError("No curl executable is installed in the container.")

    @classmethod
    def _fetch_search_page(cls, query: str) -> str:
        curl = cls._curl_executable()
        url = f"{ANIDB_BASE_URL}/browse?q={quote_plus(query)}"
        command = [
            curl,
            "-sS",
            "-L",
            "-A",
            ANIDB_USER_AGENT,
            "--max-time",
            "15",
            url,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        body = completed.stdout or ""
        if completed.returncode != 0 or not body:
            raise ResolverError(
                "The anime search provider is temporarily unavailable."
            )
        if "Just a moment..." in body or "cf-chl-" in body or "Cloudflare" in body:
            raise ResolverError(
                "The anime search provider is temporarily unavailable."
            )
        return body

    async def get_episode_count(self, anime_id: str) -> int:
        """Return the highest numbered episode indexed by AniDB for an anime."""
        match = re.search(r"-(\d+)$", str(anime_id))
        if not match:
            raise ResolverError("That anime does not have a valid episode guide.")

        try:
            body = await asyncio.to_thread(self._fetch_episode_page, match.group(1))
        except ResolverError:
            raise
        except Exception as exc:
            raise ResolverError("The episode guide is temporarily unavailable.") from exc

        numbers = [int(value) for value in re.findall(r'"number"\s*:\s*(\d+)', body)]
        if not numbers:
            raise ResolverError("No episodes were found for that anime.")
        return max(numbers)

    @classmethod
    def _fetch_episode_page(cls, numeric_anime_id: str) -> str:
        curl = cls._curl_executable()
        url = f"{ANIDB_BASE_URL}/api/frontend/anime/{numeric_anime_id}/episodes"
        command = [
            curl,
            "-sS",
            "-L",
            "-A",
            ANIDB_USER_AGENT,
            "--max-time",
            "15",
            url,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        body = completed.stdout or ""
        if completed.returncode != 0 or not body:
            raise ResolverError("The episode guide is temporarily unavailable.")
        if "Just a moment..." in body or "cf-chl-" in body or "Cloudflare" in body:
            raise ResolverError("The episode guide is temporarily unavailable.")
        return body

    async def resolve_english_dub(
        self,
        query: str,
        result_index: int,
        episode: int,
    ) -> str:
        query = query.strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
            raise ResolverError("Invalid anime title.")
        if not 1 <= result_index <= MAX_RESULT_INDEX:
            raise ResolverError("That search result is no longer valid. Search again.")
        if not 1 <= episode <= MAX_EPISODE:
            raise ResolverError("Please provide a valid episode number.")

        async with self._semaphore:
            return await asyncio.to_thread(
                self._run_ani_cli,
                query,
                result_index,
                episode,
            )

    @staticmethod
    def _run_ani_cli(query: str, result_index: int, episode: int) -> str:
        env = {
            **os.environ,
            "ANI_CLI_PLAYER": "debug",
            "ANI_CLI_MODE": "dub",
            "ANI_CLI_QUALITY": "best",
            "ANI_CLI_LOG": "0",
            "ANI_CLI_MENU": "fzf",
        }

        # Passing a list avoids shell interpretation of Telegram input.
        command = [
            "ani-cli",
            "--select-nth",
            str(result_index),
            "--episode",
            str(episode),
            "--dub",
            query,
        ]

        try:
            completed = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=settings.resolver_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ResolverError("ani-cli is not installed in the container.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ResolverError("The English-dub lookup timed out. Please try again.") from exc

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        combined = f"{stdout}\n{stderr}"

        if "No sources found for dub" in combined:
            raise ResolverError("No English dub is available for that episode.")
        if "Episode not released" in combined:
            raise ResolverError("That episode is not available yet.")
        if "No results found" in combined:
            raise ResolverError("No matching anime result was found.")
        if "Blocked by cloudflare" in combined or "Just a moment" in combined:
            raise ResolverError("The anime provider blocked the resolver. Please try again later.")

        match = re.search(
            r"Selected link:\s*\n(?P<url>https?://[^\s\x1b]+)",
            stdout,
            flags=re.IGNORECASE,
        )
        if completed.returncode != 0 or not match:
            raise ResolverError("No usable English-dub link was returned.")

        url = match.group("url").rstrip(".,);]")
        if not url.startswith(("https://", "http://")):
            raise ResolverError("The resolver returned an invalid link.")
        return url


__all__ = ["AniCliResolver", "AnimeResult", "ResolverError"]
