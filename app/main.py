from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from .bot import build_application
from .config import settings
from .resolver import AniCliResolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
# HTTPX logs full request URLs at INFO, which would expose Telegram bot tokens.
# Keep these client logs quiet in production.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

resolver = AniCliResolver()
telegram_app = build_application(settings.bot_token, resolver)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()

    if settings.public_base_url:
        await telegram_app.bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.webhook_secret,
            drop_pending_updates=True,
        )
        logger.info("Telegram webhook configured")
    else:
        logger.warning(
            "PUBLIC_BASE_URL is not set; the service is healthy but Telegram webhook "
            "has not been configured."
        )

    try:
        yield
    finally:
        # Keep the webhook during normal shutdown/redeploys. Railway may restart
        # the container briefly, and Telegram will retry delivery automatically.
        await telegram_app.stop()
        await telegram_app.shutdown()
        await resolver.close()


app = FastAPI(title="English Dub Telegram Bot", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post(settings.webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if settings.webhook_secret and x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc

    return {"ok": True}
