# PolymarketBot — headless CLI + web dashboard for Linux/Windows/macOS via Docker.
# Native pywebview desktop path (app.py, build.sh) is not included here; use the
# .app build on macOS if you want the native window.

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CONFIG_HOME=/data \
    DASHBOARD_PORT=8080 \
    ALLOW_REMOTE_DASHBOARD=true \
    POLYBOT_RUNTIME_MODE=cli

WORKDIR /app

# Build deps only needed during pip install of py-clob-client's native wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docker.txt ./
RUN pip install -r requirements-docker.txt \
    && apt-get purge -y --auto-remove build-essential libffi-dev

# Copy only what the CLI + dashboard need. app.py, build.sh, updater.py are
# desktop-only and excluded via .dockerignore / this selective copy.
COPY main.py config.py app_config.py version.py models.py utils.py \
     market_discovery.py market_data.py arbitrage.py trading.py \
     bot_state.py dashboard_server.py trade_log.py notifications.py ./
COPY dashboard/ ./dashboard/

# Non-root user; /data is the writable config + logs volume.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /data \
    && chown -R bot:bot /app /data
USER bot

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

CMD ["python", "main.py"]
