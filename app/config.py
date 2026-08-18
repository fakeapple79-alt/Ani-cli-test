from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin


@dataclass(frozen=True)
class Settings:
    bot_token: str
    public_base_url: str
    webhook_path: str
    webhook_secret: str
    resolver_timeout_seconds: int
    max_concurrent_resolvers: int

    @property
    def webhook_url(self) -> str:
        return urljoin(self.public_base_url.rstrip("/") + "/", self.webhook_path.lstrip("/"))


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required")

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    webhook_path = os.getenv("WEBHOOK_PATH", "/telegram/webhook").strip()
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()

    if public_base_url and not public_base_url.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL must start with https://")
    if public_base_url and not webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET is required when PUBLIC_BASE_URL is set")
    if webhook_secret and (
        len(webhook_secret) > 256
        or not re.fullmatch(r"[A-Za-z0-9_-]+", webhook_secret)
    ):
        raise RuntimeError(
            "WEBHOOK_SECRET may contain only letters, numbers, underscores, and hyphens"
        )

    return Settings(
        bot_token=bot_token,
        public_base_url=public_base_url,
        webhook_path=webhook_path if webhook_path.startswith("/") else f"/{webhook_path}",
        webhook_secret=webhook_secret,
        resolver_timeout_seconds=int(os.getenv("RESOLVER_TIMEOUT_SECONDS", "50")),
        max_concurrent_resolvers=int(os.getenv("MAX_CONCURRENT_RESOLVERS", "2")),
    )


settings = load_settings()


# These are intentionally not secrets. They are the public endpoints used by
# the current ani-cli implementation for search and final URL resolution.
ANIDB_BASE_URL = "https://anidb.app"
ANIDB_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/124.0.0.0 Safari/537.36"
)

MAX_SEARCH_RESULTS = 8
MAX_QUERY_LENGTH = 100
MAX_EPISODE = 10000
MAX_RESULT_INDEX = 50
SEARCH_STATE_TTL_SECONDS = 900

# Never print BOT_TOKEN, WEBHOOK_SECRET, or resolved media URLs in logs.
