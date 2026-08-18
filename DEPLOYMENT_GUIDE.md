# Deployment checklist

## 1. Rotate the Telegram token

The token previously pasted in chat is compromised. In Telegram, open `@BotFather`, use `/revoke` for the old token, then use `/token` to generate a replacement. Do not put the replacement in this file, GitHub, the Dockerfile, or chat.

## 2. Put the project contents in GitHub

Do not upload the ZIP as the only file. Extract the ZIP first. The GitHub repository root must contain `Dockerfile`, `requirements.txt`, `.env.example`, `.dockerignore`, `README.md`, and the `app/` directory directly.

The most reliable method is the Git command line:

```bash
unzip english-dub-telegram-bot.zip
cd english-dub-telegram-bot

git init
git branch -M main
git add .
git commit -m "Initial English-dub Telegram bot"

git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

Create the GitHub repository first as an empty private repository. Do not create an extra README there, otherwise the first push may require a merge. There is no `.env` file in this project and no real token should ever be added.

## 3. Connect the repository to Railway

In Railway, choose **New Project**, then **Deploy from GitHub Repo**, connect your GitHub account if asked, select the repository, and deploy it. Railway detects the root `Dockerfile` and builds the container.

If the repository contains the project inside an extra nested folder, Railway may not find the Dockerfile. The Dockerfile must be at the repository root, or you must configure Railway’s root directory to the folder that contains it.

## 4. Add Railway service variables

Open the deployed service’s **Variables** tab and add these variables to the service that runs the bot:

```text
BOT_TOKEN=<the newly generated BotFather token>
PUBLIC_BASE_URL=https://<your-generated-railway-domain>
WEBHOOK_PATH=/telegram/webhook
WEBHOOK_SECRET=<long random letters-numbers-hyphens secret>
RESOLVER_TIMEOUT_SECONDS=50
MAX_CONCURRENT_RESOLVERS=2
```

Do not paste the token into the repository. Railway variables are injected into the running service as environment variables. After changing variables, review and deploy the staged changes.

## 5. Generate the public domain

Open the Railway service’s **Settings**, find **Networking → Public Networking**, and click **Generate Domain**. Copy the resulting HTTPS domain into `PUBLIC_BASE_URL` without a trailing slash. For example:

```text
PUBLIC_BASE_URL=https://english-dub-bot-production.up.railway.app
```

The application’s webhook will then be:

```text
https://english-dub-bot-production.up.railway.app/telegram/webhook
```

Set the Railway healthcheck path to `/health` if the service settings expose a healthcheck field.

## 6. Redeploy and check logs

After all variables are present, click **Deploy** or redeploy the latest commit. A healthy startup should show a log similar to:

```text
Telegram webhook configured
```

Open the generated domain’s `/health` URL. It should return:

```json
{"ok":true}
```

## 7. Test the bot

Open the bot in Telegram and send an anime title. Tap a result, send an episode number, and confirm that the response contains an English-dub link. If no English dub exists, the bot should say so instead of falling back to subtitles.

## If you prefer not to use GitHub

The lighter alternative is Railway’s CLI. From the extracted project directory, run `railway init`, link the project and service, then run `railway up`. You still must add `BOT_TOKEN`, `PUBLIC_BASE_URL`, `WEBHOOK_PATH`, `WEBHOOK_SECRET`, `RESOLVER_TIMEOUT_SECONDS`, and `MAX_CONCURRENT_RESOLVERS` through Railway variables. GitHub is recommended because every future commit can trigger an auditable redeploy.
