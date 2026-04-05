# Polymarket BTC 15-Min Trading Bot

A Python bot that monitors Polymarket's 15-minute Bitcoin Up/Down prediction markets, detects arbitrage opportunities, and supports directional trading with a live web dashboard.

## Features

- **Market Discovery** — Automatically finds the current active 15-min BTC market via the Gamma API with three fallback strategies
- **Real-Time Prices** — WebSocket streaming with REST fallback for live bid/ask data
- **Arbitrage Detection** — Calculates if buying both YES + NO is profitable after fees, with order book depth analysis and slippage-aware sizing; toggled by `ARB_ENABLED`
- **Directional Trading** — Configurable price triggers to buy YES or NO when thresholds are hit
- **Live Web Dashboard** — Browser-based UI with real-time prices, arbitrage status, trade history, live PnL tracking, and bot settings
- **Trade Detail Panel** — Clickable trade rows open a slide-out drawer with entry context, per-trade price chart (before and after entry), and resolution summary
- **Live PnL Tracking** — Unrealized PnL updates every polling cycle based on current bid prices; trades resolve automatically when markets expire
- **Spike Filter** — Detects and rejects anomalous price jumps by cross-validating against REST before accepting the tick
- **Auto-Updater** — Background GitHub Releases check on startup; supports stable and beta update channels, configurable from the settings drawer
- **Telegram Notifications** — Optional alerts for market switches, arb detections, and trade executions
- **DRY RUN Mode** — Enabled by default, simulates trades with no real money at risk
- **Proxy Wallet Support** — Works with Google/email Polymarket accounts

## Quick Start

### Desktop App (macOS)

```bash
# 1. Install dependencies (Python 3.13+)
pip install -r requirements.txt

# 2. Launch the desktop app
python app.py
```

On first launch, a setup wizard walks you through wallet credentials, Telegram alerts, and trading parameters. Config is stored at `~/Library/Application Support/PolymarketBot/config.json` — no `.env` file needed.

To build a distributable `.app` bundle and `.dmg` installer:

```bash
bash build.sh
# Output: dist/PolymarketBot.app  and  dist/PolymarketBot.dmg
```

Requires `pyinstaller` and optionally `create-dmg` (`brew install create-dmg`).

### CLI

```bash
# 1. Install dependencies (Python 3.13+)
pip install -r requirements.txt

# 2. Copy and fill in your .env
cp .env.example .env
# Edit .env with your private key and funder address

# 3. Run the bot (dry run mode by default)
python main.py
```

The web dashboard starts automatically at `http://localhost:8080`.

## Commands

```bash
python app.py                # Launch the native macOS desktop app (recommended)
python app.py --cli          # Fall back to CLI mode from the desktop entry point
python main.py               # Run the CLI bot with web dashboard
python main.py --scan        # Scan for active BTC 15-min markets
python main.py --arb-check   # One-shot arbitrage check on current market
```

## Configuration

All config is in `.env` (see `.env.example` for all options):

| Variable | Default | Description |
|---|---|---|
| `PRIVATE_KEY` | — | Your Polygon wallet private key |
| `FUNDER_ADDRESS` | — | Proxy wallet address (from polymarket.com/settings) |
| `SIGNATURE_TYPE` | `2` | `0`=EOA, `1`=MagicLink, `2`=Google/proxy |
| `DRY_RUN` | `true` | Set to `false` to enable real trading |
| `POLLING_INTERVAL` | `5` | Seconds between price refreshes |
| `AUTO_EXECUTE` | `false` | Automatically execute arb trades when detected |
| `MAX_POSITION_SIZE` | `100` | Max shares per trade execution |
| `ARB_COOLDOWN_SECONDS` | `120` | Min seconds between executions on the same market |
| `ARB_MIN_PROFIT` | `0.005` | Min profit ($) to flag an arb |
| `ARB_MIN_ROI_PCT` | `0.3` | Min ROI (%) to flag an arb |
| `USE_WEBSOCKET` | `true` | Enable real-time WebSocket price streaming |
| `SPIKE_THRESHOLD` | `0.15` | Max price jump (0-1) before REST confirmation is required |
| `BUY_YES_TRIGGER` | `0` | Buy YES when price hits this threshold (0 = disabled) |
| `BUY_NO_TRIGGER` | `0` | Buy NO when price hits this threshold (0 = disabled) |
| `DIRECTIONAL_BUY_SIZE` | `50` | Shares per directional buy |
| `ARB_ENABLED` | `true` | Master switch for arbitrage detection |
| `MARKET_REST_SECONDS` | `0` | Wait period after a new market opens before trading |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token (from @BotFather) |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID (from @userinfobot) |

Most settings can also be changed at runtime via the web dashboard's settings panel.

## How Arbitrage Works

On a binary market (YES/NO), the guaranteed payout is $1.00 per share. If you can buy both sides for less than $1.00 total, you lock in a risk-free profit:

```
profit = $1.00 - (YES_ask + NO_ask) - fees
```

Polymarket uses a sliding-scale fee: `fee = shares * feeRate * p * (1 - p)`, where fees peak at 50/50 odds and drop near certainty.

The bot performs two levels of analysis:
1. **Quick check** — 1-share test at best ask prices
2. **Depth analysis** — Walks the order book to compute VWAP fill prices and find the maximum profitable position size after slippage

## Spike Filter

WebSocket and REST prices can occasionally spike to extreme values (e.g. 99c) due to thin order books or stale data. The spike filter:

1. Tracks the last accepted price for each token
2. If a new tick jumps more than `SPIKE_THRESHOLD` from the last price, fires a REST API call to cross-validate
3. Accepts the price only if REST confirms the move; otherwise keeps the last known good price
4. Resets automatically when the market rotates to a new 15-minute window

## Architecture

```
app.py                 → Desktop entry point (pywebview + Flask native window)
├── app_config.py      → JSON config system (~/Library/Application Support/...)
├── updater.py         → Background GitHub Releases update checker
├── version.py         → Single VERSION constant and GitHub repo URL
├── dashboard/setup.html → First-run setup wizard (6 steps)
main.py                → CLI entry point, polling loop & terminal dashboard
├── config.py          → Environment config (.env / overridden by app_config)
├── market_discovery.py → Find active 15-min BTC markets (Gamma API)
├── market_data.py     → Fetch prices, order books & WebSocket streaming
├── arbitrage.py       → Arbitrage detection math & fee calculation
├── trading.py         → Order execution via py-clob-client
├── bot_state.py       → Thread-safe shared state (bot <-> dashboard bridge)
├── dashboard_server.py → Flask web server with SSE for real-time updates
├── dashboard/         → Web dashboard (HTML/CSS/JS)
├── trade_log.py       → Trade logging to file
├── notifications.py   → Telegram notification support
├── models.py          → Data classes (Market, OrderBook, PriceSnapshot, etc.)
└── utils.py           → Logging & formatting helpers
```

### Desktop App vs CLI

`app.py` is the primary entry point for the macOS desktop app. It starts a Flask server on port 8089 in a background thread, opens a native pywebview window, and manages the bot lifecycle through a GUI. Config is read from/written to JSON rather than `.env`.

`main.py` remains fully functional as a standalone CLI tool. When called from `app.py`, it runs in a daemon thread with a `stop_event` for clean shutdown. The `--cli` flag on `app.py` bypasses the GUI and delegates to `main.py` directly.

## Key External APIs

- **Gamma API** (`gamma-api.polymarket.com`) — Market metadata, search, tags
- **CLOB API** (`clob.polymarket.com`) — Order books, prices, fee rates, order placement
- **CLOB WebSocket** (`ws-subscriptions-clob.polymarket.com`) — Real-time price streaming
- **py-clob-client** — Polymarket's Python SDK for authenticated trading

## Disclaimer

This bot is for educational and research purposes. Prediction markets involve financial risk. Always start in DRY RUN mode and use funds you can afford to lose.
