# English-Dub Telegram Anime Bot

This bot runs as a FastAPI webhook service on Railway. Users send an anime title, choose a search result, enter an episode number, and receive a freshly resolved link from `ani-cli` in **dub mode**. The bot never launches a media player and never downloads or stores video files.

## Important security action

The Telegram token previously exposed in chat must be revoked in `@BotFather` before deployment. Use `/revoke`, then generate a replacement with `/token`. Put the replacement only in Railway’s private `BOT_TOKEN` variable. Never place it in Python files, the Dockerfile, Git, `.env.example`, or a Telegram message.

## Project files

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI app, Railway health endpoint, Telegram webhook registration, and request verification. |
| `app/bot.py` | `/start`, `/cancel`, title search, inline result buttons, episode input, and user-facing errors. |
| `app/resolver.py` | anidb search plus safe `ani-cli --dub` subprocess execution. |
| `app/config.py` | Environment-variable loading and validation. |
| `Dockerfile` | Installs shell dependencies, ani-cli, Python dependencies, and starts Uvicorn. |
| `requirements.txt` | Pinned Python packages. |
| `.env.example` | Placeholder configuration only; it contains no real secret. |

## Local test

Copy the template to a local `.env` file and fill it with a newly generated token. Do not commit `.env`.

```bash
cp .env.example .env
# edit .env with your local values

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a
. ./.env
set +a
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

For a local webhook test, use a public HTTPS tunnel or use Railway directly. Opening `http://127.0.0.1:8000/health` should return `{ "ok": true }`.

## Deploy to Railway

Create a new Railway project from this repository. Railway detects the `Dockerfile` automatically. The image installs `ani-cli` from the `ANI_CLI_REF` build argument, along with Bash, curl, fzf, grep, sed, coreutils, ncurses `tput`, and certificates. Debug mode does not require mpv, VLC, ffmpeg, or yt-dlp.

After the first deployment, create or copy the service’s public HTTPS domain. Then set these service variables:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | The newly generated token from `@BotFather`; never the revoked token. |
| `PUBLIC_BASE_URL` | Your Railway HTTPS domain, such as `https://your-service.up.railway.app`. |
| `WEBHOOK_PATH` | `/telegram/webhook` |
| `WEBHOOK_SECRET` | A long random string containing letters, numbers, underscores, or hyphens. |
| `RESOLVER_TIMEOUT_SECONDS` | `50` |
| `MAX_CONCURRENT_RESOLVERS` | `2` |

Redeploy after adding the variables. On startup, the application calls Telegram’s `setWebhook` with:

```text
https://your-service.up.railway.app/telegram/webhook
```

The application verifies Telegram’s `X-Telegram-Bot-Api-Secret-Token` header before processing an update. Set the Railway healthcheck path to `/health` if the service settings provide a healthcheck field.

## How English-dub resolution works

The resolver runs a command equivalent to this for each request:

```bash
ANI_CLI_PLAYER=debug \
ANI_CLI_MODE=dub \
ANI_CLI_QUALITY=best \
ani-cli --select-nth 2 --episode 5 --dub "One Piece"
```

The bot parses only the URL after `Selected link:`. If ani-cli reports `No sources found for dub!`, the bot tells the user that no English dub is available. It never falls back to subtitle mode.

The subprocess uses an argument list rather than `shell=True`, applies a timeout, limits concurrent resolutions, and validates the returned URL. Search and resolved links are not written to logs.

## First-use flow in Telegram

Send a title such as `One Piece`. Tap one of the returned search buttons. Send an episode number such as `5`. The bot returns the current English-dub link. Provider links may expire, so users should request a fresh link if an old one stops working.

## Troubleshooting

If Railway reports that `BOT_TOKEN` is missing, check that the variable was added to the **service** that is actually deploying the Dockerfile, not merely to the project or another service. Variable names are case-sensitive.

If the deployment is healthy but Telegram sends nothing, verify that `PUBLIC_BASE_URL` is the deployed HTTPS domain with no trailing path, that `WEBHOOK_SECRET` is set, and that the service has been redeployed after both variables were added. Check Railway logs for `Telegram webhook configured`.

If `/health` works but episode resolution fails, inspect the error message sent by the bot. Typical causes are no English dub for that episode, an upstream provider change, an upstream anti-bot block, or a timeout. Do not silently switch to subtitle mode.

If search returns no titles, the provider’s HTML layout may have changed or may be blocking the Railway IP. The search parser mirrors the current ani-cli endpoint and should be treated as an adapter that may need maintenance.

## Operational notes

Pin `ANI_CLI_REF` to a tested tag or commit for production instead of allowing the Docker build to follow `master` indefinitely. Resolve links on demand rather than storing them. Do not add a volume unless you later need small application state; this design does not store video files.

Review the terms and rights applicable to any upstream source before making the bot public. The project is a link resolver and does not itself establish permission to redistribute third-party media.
