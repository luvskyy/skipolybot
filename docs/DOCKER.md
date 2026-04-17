# Run with Docker (any OS)

The Docker path runs the bot's **CLI core + web dashboard** inside a container.
It works on macOS, Linux, and Windows via Docker Desktop or Docker Engine. The
native pywebview window is not available here — you drive the bot through your
browser at `http://localhost:8080`.

If you're on macOS and want the native `.app`, use `python app.py` or `bash
build.sh` instead — the Docker image is for portability, not a replacement.

---

## Prerequisites

- Docker Desktop (macOS / Windows) or Docker Engine + Compose plugin (Linux)
- A Polymarket proxy wallet. See the main [README](../README.md) for how to get
  your private key + funder address.

## Quick start

```bash
# 1. Clone and configure
git clone https://github.com/luvskyy/skipolybot.git
cd skipolybot
cp .env.example .env
# edit .env: set PRIVATE_KEY, FUNDER_ADDRESS, leave DRY_RUN=true for first run

# 2. Build + run
docker compose up -d

# 3. Open the dashboard
open http://localhost:8080   # macOS
# or just paste it into your browser
```

Stop the bot:

```bash
docker compose down
```

Follow logs:

```bash
docker compose logs -f bot
```

## What persists

`docker-compose.yml` mounts `./data` → `/data` inside the container. Everything
the bot writes to disk lives there:

```
data/
└── PolymarketBot/
    ├── config.json      # dashboard-managed settings
    └── logs/            # rotating bot logs
```

Rebuilding the image does not touch this directory. Delete it to start fresh.

## Configuration

Two layers, same as the native build:

1. **Wallet + secrets** — loaded from `.env` at container start via
   `env_file:`. Safer than baking them into the image. Never commit `.env`.
2. **Runtime toggles** — editable live through the dashboard's Settings drawer.
   Changes save to `data/PolymarketBot/config.json`.

Desktop-only features (auto-update, uninstall, beta channel) are hidden in the
Docker dashboard. Upgrade by pulling the repo and rebuilding:

```bash
git pull
docker compose build --pull
docker compose up -d
```

## Security notes

- The dashboard has **no authentication**. `docker-compose.yml` binds the port
  to `127.0.0.1:8080:8080`, so only the host machine reaches it. If you change
  that to `8080:8080` you expose the dashboard to your LAN — don't, unless you
  add a reverse proxy with auth in front.
- Wallet keys are loaded from `.env` at runtime only. The image itself is
  stateless and contains no secrets (confirm with `docker history polymarketbot`).
- `ALLOW_REMOTE_DASHBOARD=true` inside the container allows traffic through the
  Docker bridge. The per-request Origin/Referer CSRF check still enforces that
  state-changing requests come from a browser pointed at `localhost`.

## Multi-arch builds

If you build on Apple Silicon but deploy to an amd64 host (or vice versa),
build a multi-arch image with buildx:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t polymarketbot:latest --load .
```

(Use `--push` instead of `--load` if you're pushing to a registry that holds
both manifests.)

## Troubleshooting

**`docker compose up` exits immediately** — check `docker compose logs bot`.
Most often: missing `PRIVATE_KEY` / `FUNDER_ADDRESS` in `.env`.

**Dashboard shows "Loading…" forever** — the bot process crashed. Check logs.
Confirm the container is healthy with `docker ps` (STATUS column).

**Can't reach `http://localhost:8080`** — verify the port binding:
`docker compose port bot 8080` should print `127.0.0.1:8080`. On Windows check
Docker Desktop has the port published.

**"Dry run" stays on even after I turn it off** — the dashboard edits
`config.json` but the bot reads `.env` at start too. Set `DRY_RUN=false` in
`.env` and restart (`docker compose restart bot`).
