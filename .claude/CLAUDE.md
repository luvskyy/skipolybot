# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python bot that monitors Polymarket's 15-minute Bitcoin Up/Down prediction markets and detects arbitrage opportunities. It targets binary markets where buying both YES + NO for less than $1.00 (minus fees) locks in risk-free profit.

## Commands

```bash
# Install dependencies (uses venv with Python 3.13)
pip install -r requirements.txt

# Run the bot (live polling loop, DRY RUN by default)
python main.py

# Scan for active BTC 15-min markets
python main.py --scan

# One-shot arbitrage check on current market
python main.py --arb-check
```

No test suite or linter is configured.

## Architecture

The bot follows a simple pipeline: discover market -> fetch prices -> detect arbitrage -> (optionally) execute trades.

- **config.py** — Loads `.env` via python-dotenv. All settings are module-level constants. `config.validate()` checks wallet config on startup.
- **models.py** — Dataclasses: `Market`, `OrderBook` (with `OrderBookLevel`/`OrderBookSide`), `PriceSnapshot`, `ArbitrageOpportunity`. No ORM or database.
- **market_discovery.py** — Finds active 15-min BTC markets via the Gamma API (`gamma-api.polymarket.com`). Uses three fallback strategies: keyword search, tag-based search, then broad active scan. Fetches fee rates from the CLOB API.
- **market_data.py** — Fetches order books and prices from the CLOB API (`clob.polymarket.com`). `compute_fill_price()` walks the book for VWAP/slippage estimation. `MarketWebSocket` streams real-time prices via WebSocket in a daemon thread; `fetch_price_snapshot_hybrid()` uses WS prices when fresh, falls back to REST.
- **arbitrage.py** — Core math. Fee formula: `shares * feeRate * p * (1-p)` (fees peak at 50/50 odds). `detect_arbitrage()` does a simple 1-share check; `detect_arbitrage_with_depth()` accounts for order book slippage; `find_max_profitable_size()` binary-searches for the largest profitable position.
- **trading.py** — `TradingClient` wraps `py-clob-client`. Supports limit (GTC) and market (FOK) orders. All methods no-op with logging in DRY_RUN mode. `execute_arbitrage()` places paired YES+NO limit orders.
- **main.py** — CLI entry point with argparse. The polling loop (`run_bot()`) cycles through discovery, price fetch, arb detection, and dashboard rendering. Terminal dashboard uses ANSI escape codes for a boxed UI.
- **utils.py** — Logger setup (`polybot` logger), time helpers for 15-min windows, and formatting functions.

## Key External APIs

- **Gamma API** (`gamma-api.polymarket.com`) — Market metadata, search, tags
- **CLOB API** (`clob.polymarket.com`) — Order books, prices, fee rates, order placement
- **py-clob-client** — Polymarket's Python SDK for authenticated trading (order creation, signing, posting)

## Configuration

All runtime config comes from `.env` (see `.env.example`). Key settings:
- `PRIVATE_KEY` / `FUNDER_ADDRESS` — Polygon wallet credentials for proxy wallet auth
- `SIGNATURE_TYPE` — 0=EOA, 1=MagicLink, 2=Google/proxy (default 2)
- `DRY_RUN` — `true` by default; must be explicitly set to `false` for real trading
- `ARB_MIN_PROFIT` / `ARB_MIN_ROI_PCT` — Thresholds for flagging opportunities
- `AUTO_EXECUTE` — `false` by default; enables automatic trade execution when arb detected
- `MAX_POSITION_SIZE` — Max shares per arb execution (default 100)
- `ARB_COOLDOWN_SECONDS` — Minimum seconds between executions on the same market (default 120)
- `USE_WEBSOCKET` — `true` by default; enables real-time price streaming (falls back to REST polling)
