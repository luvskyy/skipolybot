# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python bot that monitors Polymarket's 15-minute Bitcoin Up/Down prediction markets and detects arbitrage opportunities. It targets binary markets where buying both YES + NO for less than $1.00 (minus fees) locks in risk-free profit.

The project ships as both a native macOS desktop app (`app.py` / `build.sh`) and a headless CLI tool (`main.py`).

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

No test suite or linter is configured.

## Architecture

Two entry points share the same bot core:

- **app.py** — Desktop entry point. Launches Flask on port 8089 in a background thread, opens a native `pywebview` window, and manages the bot via `AppBridge` (Python/JS API). Routes: `/setup`, `/api/setup/save`, `/api/config`, `/api/bot/start`, `/api/bot/stop`, `/api/update-status`, `/api/update-channel`, `/api/update-check`, `/api/uninstall`. On first run, opens `setup.html` (setup wizard); otherwise loads the main dashboard.
- **main.py** — CLI entry point with argparse. The polling loop (`run_bot()`) cycles through discovery, price fetch, arb detection, and dashboard rendering. Terminal dashboard uses ANSI escape codes for a boxed UI. Accepts a `stop_event` for clean shutdown when called from `app.py`.

Shared core:

- **config.py** — Loads `.env` via python-dotenv. All settings are module-level constants. `config.validate()` checks wallet config on startup. `ARB_ENABLED` gates the arb detection block in `main.py`.
- **app_config.py** — JSON-based config stored at `~/Library/Application Support/PolymarketBot/config.json`. `DEFAULTS` dict defines all keys and defaults. `apply_config_to_module()` bridges JSON config into `config.py` module-level constants at startup and on settings save. `market_rest_seconds` defaults to `480` (8 minutes).
- **version.py** — Single `VERSION` constant (`1.1.0`) and GitHub Releases URLs (`GITHUB_RELEASES_URL` for stable, `GITHUB_ALL_RELEASES_URL` for all releases including betas).
- **updater.py** — Background GitHub Releases checker. Supports `"stable"` (non-prerelease only) and `"beta"` (all releases) channels. `set_channel()` / `get_channel()` switch channels at runtime. `check_for_update()` is re-entrant safe via `_lock` and sets `UpdateStatus.checking = True` while in-flight. `start_update_check()` fires a background thread on app startup.
- **models.py** — Dataclasses: `Market`, `OrderBook` (with `OrderBookLevel`/`OrderBookSide`), `PriceSnapshot`, `ArbitrageOpportunity`. No ORM or database.
- **market_discovery.py** — Finds active 15-min BTC markets via the Gamma API (`gamma-api.polymarket.com`). Uses three fallback strategies: keyword search, tag-based search, then broad active scan. Fetches fee rates from the CLOB API.
- **market_data.py** — Fetches order books and prices from the CLOB API (`clob.polymarket.com`). `compute_fill_price()` walks the book for VWAP/slippage estimation. `MarketWebSocket` streams real-time prices via WebSocket in a daemon thread; `fetch_price_snapshot_hybrid()` uses WS prices when fresh, falls back to REST.
- **arbitrage.py** — Core math. Fee formula: `shares * feeRate * p * (1-p)` (fees peak at 50/50 odds). `detect_arbitrage()` does a simple 1-share check; `detect_arbitrage_with_depth()` accounts for order book slippage; `find_max_profitable_size()` binary-searches for the largest profitable position.
- **trading.py** — `TradingClient` wraps `py-clob-client`. Supports limit (GTC) and market (FOK) orders. All methods no-op with logging in DRY_RUN mode. `execute_arbitrage()` places paired YES+NO limit orders.
- **bot_state.py** — Thread-safe singleton (`state`) bridging the bot loop and the web dashboard. `TradeRecord` dataclass holds rich entry context (time remaining, fee rate, liquidity, per-trade price history). Each trade gets an incrementing `trade_id`. `get_trade_detail(trade_id)` returns full detail including `price_history_before` and `price_history_after`. `resolve_trades()` accepts an optional `winning_side` for directional PnL calculation.
- **dashboard_server.py** — Flask server (default port 8080 for CLI, 8089 for desktop app). SSE at `/api/stream` pushes state updates. `GET /api/trade/<trade_id>` returns per-trade detail with price history. `POST /api/settings` validates and applies runtime config changes.
- **dashboard/** — Web dashboard. `index.html` contains the main UI and two slide-out drawers: settings (with update channel selector and manual check button) and trade detail. `app.js` manages SSE, chart rendering, settings panel, trade detail panel (with per-trade price chart), and update checks. `setup.html` is the first-run setup wizard.
- **utils.py** — Logger setup (`polybot` logger), time helpers for 15-min windows, and formatting functions.

## Key External APIs

- **Gamma API** (`gamma-api.polymarket.com`) — Market metadata, search, tags
- **CLOB API** (`clob.polymarket.com`) — Order books, prices, fee rates, order placement
- **CLOB WebSocket** (`ws-subscriptions-clob.polymarket.com`) — Real-time price streaming
- **GitHub Releases API** — `https://api.github.com/repos/luvskyy/skipolybot/releases` — update checks
- **py-clob-client** — Polymarket's Python SDK for authenticated trading (order creation, signing, posting)

## Configuration

The desktop app reads/writes `~/Library/Application Support/PolymarketBot/config.json`. The CLI reads `.env` (see `.env.example`). `app_config.apply_config_to_module()` bridges both paths into the same `config.py` constants.

Key settings:
- `PRIVATE_KEY` / `FUNDER_ADDRESS` — Polygon wallet credentials for proxy wallet auth
- `SIGNATURE_TYPE` — 0=EOA, 1=MagicLink, 2=Google/proxy (default 2)
- `DRY_RUN` — `true` by default; must be explicitly set to `false` for real trading
- `ARB_ENABLED` — `true` by default; master switch for arbitrage detection
- `ARB_MIN_PROFIT` / `ARB_MIN_ROI_PCT` — Thresholds for flagging opportunities
- `AUTO_EXECUTE` — `false` by default; enables automatic trade execution when arb detected
- `MAX_POSITION_SIZE` — Max shares per arb execution (default 100)
- `ARB_COOLDOWN_SECONDS` — Minimum seconds between executions on the same market (default 120)
- `MARKET_REST_SECONDS` — Seconds to skip trading after a new market opens (default 480 in app_config, 0 in .env)
- `USE_WEBSOCKET` — `true` by default; enables real-time price streaming (falls back to REST polling)
- `SPIKE_THRESHOLD` — Max allowed price jump before REST confirmation is required (default 0.15)
