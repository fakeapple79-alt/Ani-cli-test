FROM python:3.12-slim-bookworm

ARG ANI_CLI_REF=master
ARG CURL_IMPERSONATE_VERSION=0.6.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANI_CLI_PLAYER=debug \
    ANI_CLI_MODE=dub \
    ANI_CLI_QUALITY=best \
    ANI_CLI_MENU=fzf \
    ANI_CLI_LOG=0

WORKDIR /app

# ani-cli debug mode does not need mpv, VLC, ffmpeg, or yt-dlp.
# It does need shell tools, fzf, and a browser-impersonating curl because
# AniDB can return Cloudflare 403 to ordinary curl from cloud hosts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        curl \
        fzf \
        grep \
        ncurses-bin \
        sed \
        tar \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL --retry 3 \
        "https://github.com/lwthiker/curl-impersonate/releases/download/v${CURL_IMPERSONATE_VERSION}/curl-impersonate-v${CURL_IMPERSONATE_VERSION}.x86_64-linux-gnu.tar.gz" \
        -o /tmp/curl-impersonate.tar.gz \
    && tar -xzf /tmp/curl-impersonate.tar.gz -C /usr/local/bin \
    && rm -f /tmp/curl-impersonate.tar.gz \
    && curl -fsSL --retry 3 \
        "https://raw.githubusercontent.com/pystardust/ani-cli/${ANI_CLI_REF}/ani-cli" \
        -o /usr/local/bin/ani-cli \
    && chmod 0755 /usr/local/bin/ani-cli \
    && curl_chrome116 --version \
    && ani-cli --version

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY .env.example ./

RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
