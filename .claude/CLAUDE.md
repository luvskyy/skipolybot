# CLAUDE.md

Guidance Claude Code (claude.ai/code) for this repo.

## Project Overview

Python bot. Monitors Polymarket 15-min BTC Up/Down markets. Detects arb. Targets binary markets: buy YES+NO < $1.00 (minus fees) = risk-free profit.

Ships as native macOS desktop app (`app.py` / `build.sh`) + headless CLI (`main.py`).

## Commands

```bash
# Launch the native macOS desktop app (recommended)
python app.py

# Fall back to CLI mode from the desktop entry point
python app.py --cli

# Run the CLI bot with web dashboard (DRY RUN by default)
python main.py

# Scan for active BTC 15-min markets
python main.py --scan

# One-shot arbitrage check on current market
python main.py --arb-check

# Build distributable .app bundle and .dmg installer
bash build.sh
```

No tests/linter.

## Architecture

Two entry points, shared bot core:

- **app.py** — Desktop entry. Launches Flask port 8089 background thread, opens native `pywebview` window, manages bot via `AppBridge` (Python/JS API). Routes: `/setup`, `/api/setup/save`, `/api/config`, `/api/bot/start`, `/api/bot/stop`, `/api/update-status`, `/api/update-channel`, `/api/update-check`, `/api/update-download`, `/api/update-download-progress`, `/api/update-install`, `/api/suppress-beta-warning`, `/api/uninstall`. First run → `setup.html` wizard; else main dashboard.
- **main.py** — CLI entry, argparse. Poll loop (`run_bot()`): discovery → price fetch → arb detect → dashboard render. Terminal dashboard uses ANSI escapes, boxed UI. Accepts `stop_event` for clean shutdown from `app.py`.

Shared core:

- **config.py** — Loads `.env` via python-dotenv. Settings = module constants. `config.validate()` checks wallet on startup. `ARB_ENABLED` gates arb block in `main.py`.
- **app_config.py** — JSON config at `~/Library/Application Support/PolymarketBot/config.json`. `DEFAULTS` dict = all keys/defaults. `apply_config_to_module()` bridges JSON → `config.py` constants at startup + on save. `market_rest_seconds` default `480` (8 min).
- **version.py** — `VERSION` constant (`1.2.1-beta.3`) + GitHub Releases URLs (`GITHUB_RELEASES_URL` stable, `GITHUB_ALL_RELEASES_URL` all incl. betas). `DOWNLOAD_URL` = HTML fallback; real DMG URL from release assets runtime.
- **updater.py** — Background GitHub Releases checker. Channels: `"stable"` (non-prerelease), `"beta"` (all). `set_channel()`/`get_channel()` switch runtime. `check_for_update()` re-entrant safe via `_lock`, sets `UpdateStatus.checking = True` in-flight. `start_update_check()` fires bg thread on startup. `DownloadStatus` dataclass tracks DMG download. `start_download()` streams DMG to temp dir, 65KB chunks, progress tracked. `get_download_status()` returns progress dict for frontend poll. `install_and_restart()` mounts DMG via `hdiutil`, writes detached bash script copying new `.app` over current bundle + relaunches, calls `os._exit(0)` after 0.5s. `_parse_version()` appends `(0,)` for stable, `(-1, N)` for pre-release tags → tuple cmp ranks betas below stable counterpart, orders beta.2 above beta.1.
- **models.py** — Dataclasses: `Market`, `OrderBook` (w/ `OrderBookLevel`/`OrderBookSide`), `PriceSnapshot`, `ArbitrageOpportunity`. No ORM/DB.
- **market_discovery.py** — Finds active 15-min BTC markets via Gamma API (`gamma-api.polymarket.com`). 3 fallback strategies: keyword search → tag search → broad active scan. Fetches fee rates from CLOB API.
- **market_data.py** — Fetches order books + prices from CLOB API (`clob.polymarket.com`). `compute_fill_price()` walks book for VWAP/slippage. `MarketWebSocket` streams real-time prices via WS, daemon thread; `fetch_price_snapshot_hybrid()` uses WS when fresh, falls back REST. `fetch_pyth_btc_price(ttl)` — TTL-cached Pyth Hermes BTC price (display only, not used in arb math); `fetch_btc_price()` = Binance US / Coinbase fallback.
- **arbitrage.py** — Core math. Fee: `shares * feeRate * p * (1-p)` (peak at 50/50). `detect_arbitrage()` = simple 1-share check; `detect_arbitrage_with_depth()` accounts for slippage; `find_max_profitable_size()` binary-search max profitable position.
- **trading.py** — `TradingClient` wraps `py-clob-client`. Supports limit (GTC) + market (FOK). All methods no-op w/ log in DRY_RUN. `execute_arbitrage()` places paired YES+NO limit orders.
- **bot_state.py** — Thread-safe singleton (`state`), bridges bot loop + web dashboard. `TradeRecord` dataclass holds entry context (time remaining, fee rate, liquidity, per-trade price history). Each trade gets incrementing `trade_id`. `get_trade_detail(trade_id)` returns detail incl. `price_history_before`/`price_history_after`. `resolve_trades()` accepts optional `winning_side` for directional PnL. `stop_loss_trade(trade_id, realized_pnl)` marks trade `STOPPED` w/ realized loss. `update_trade_pnl()` skips (preserves last) when bid is `None` → prevents `None`→$0 inflating loss.
- **dashboard_server.py** — Flask (port 8080 CLI, 8089 desktop). SSE `/api/stream` pushes state. `GET /api/trade/<trade_id>` → per-trade detail w/ history. `POST /api/settings` validates + applies runtime config.
- **dashboard/** — Web dashboard. `index.html` = main UI + 2 slide-out drawers: settings (w/ channel selector + manual check) and trade detail. `app.js` manages SSE, chart render, settings panel, trade detail panel (per-trade chart), update checks. `setup.html` = first-run wizard.
- **utils.py** — Logger (`polybot`), 15-min window time helpers, format fns.

## Key External APIs

- **Gamma API** (`gamma-api.polymarket.com`) — Market metadata, search, tags
- **CLOB API** (`clob.polymarket.com`) — Order books, prices, fees, orders
- **CLOB WebSocket** (`ws-subscriptions-clob.polymarket.com`) — Real-time prices
- **GitHub Releases API** — `https://api.github.com/repos/luvskyy/skipolybot/releases` — update checks
- **Pyth Hermes REST** (`hermes.pyth.network/v2/updates/price/latest`) — Live BTC/USD price; feed ID `e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43`; no auth; matches Polymarket's chart oracle
- **py-clob-client** — Polymarket Python SDK, authenticated trading (create/sign/post orders)

## Configuration

Desktop app R/W `~/Library/Application Support/PolymarketBot/config.json`. CLI reads `.env` (see `.env.example`). `app_config.apply_config_to_module()` bridges both → same `config.py` constants.

Key settings:
- `PRIVATE_KEY` / `FUNDER_ADDRESS` — Polygon wallet creds, proxy wallet auth
- `SIGNATURE_TYPE` — 0=EOA, 1=MagicLink, 2=Google/proxy (default 2)
- `DRY_RUN` — `true` default; must set `false` for real trading
- `ARB_ENABLED` — `true` default; master switch arb detection
- `ARB_MIN_PROFIT` / `ARB_MIN_ROI_PCT` — Thresholds for flagging opps
- `AUTO_EXECUTE` — `false` default; auto exec when arb detected
- `MAX_POSITION_SIZE` — Max shares/arb exec (default 100)
- `ARB_COOLDOWN_SECONDS` — Min sec between execs same market (default 120)
- `MARKET_REST_SECONDS` — Sec to skip trading after new market opens (default 480 app_config, 0 .env)
- `USE_WEBSOCKET` — `true` default; real-time streaming (falls back REST)
- `SPIKE_THRESHOLD` — Max price jump before REST confirm required (default 0.15)
- `BTC_PRICE_POLL_SECONDS` — Poll interval for dashboard BTC price via Pyth (default 3.0); below 1 risks IP ban, below 3 risks rate limits
- `suppress_beta_warning` — `false` default; `true` hides "Beta updates may be unstable" banner (JSON only, not `.env`)

## Dashboard JS Conventions

- New config key = 5 touch points: `config.py` const, `app_config.py` `apply_config_to_module()`, `bot_state.py` `get_settings()`/`set_settings()`, `index.html` form input, `dashboard/assets/app.js` `SETTINGS_DEFAULTS`. Miss any → silent load/save break.
- `drawTradeChart` canvas scale: use `ctx.setTransform(dpr,0,0,dpr,0,0)` not `ctx.scale`. 1s poll repeats call → `scale` compounds DPR, `setTransform` absolute. Keep pattern.
- Trade detail drawer contract: `renderTradeDetail(t)` = skeleton once w/ stable ids (`td-pnl`, `td-roi`, `td-status`, `td-resolution-section`). `updateTradeDetailDynamic(t)` = patch those every 1s. Don't merge — skeleton first or patcher finds no ids.