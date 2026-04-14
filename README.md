# 🚀 Polymarket BTC 15-Min Trading Bot

> A sleek Python bot for monitoring Polymarket’s **15-minute Bitcoin Up/Down markets**, spotting **arbitrage opportunities**, executing **directional trades**, and visualizing everything in a **live dashboard**.

---

## ✨ Highlights

* 🔎 **Smart Market Discovery** — Automatically finds the active BTC 15-min market using the Gamma API with **3 fallback strategies**
* ⚡ **Real-Time Pricing** — WebSocket-first streaming with REST fallback for reliable bid/ask updates
* 💸 **Arbitrage Detection** — Finds profitable YES + NO opportunities with **slippage-aware sizing** and **depth analysis**
* 🎯 **Directional Trading** — Auto-buy YES or NO when custom thresholds are hit
* 📊 **Live Dashboard** — Beautiful browser UI with:

  * Real-time prices
  * Arbitrage status
  * Trade history
  * Live PnL
  * Runtime settings
  * Log export / clipboard copy
* 📈 **PnL Tracking** — Unrealized PnL updates every polling cycle and auto-resolves expired markets
* 🛡️ **Spike Filter** — Rejects bad ticks by validating suspicious jumps against REST data
* 📲 **Telegram Alerts** — Optional notifications for market switches, arb events, and trades
* 🧪 **DRY RUN by Default** — Simulate safely before risking capital
* 👛 **Proxy Wallet Support** — Compatible with Google/email Polymarket accounts
* 🔄 **In-App Updates** — Background DMG download with progress bar and one-click install & restart

---

## 🚀 Quick Start

### 🖥️ Desktop App (macOS)

```bash
# Install dependencies (Python 3.13+)
pip install -r requirements.txt

# Launch desktop app
python app.py
```

On first launch, a **guided setup wizard** helps configure:

* 👛 Wallet credentials
* 📲 Telegram alerts
* ⚙️ Trading parameters

Configuration is stored in:

```text
~/Library/Application Support/PolymarketBot/config.json
```

> ✅ No `.env` required for desktop mode.

### 📦 Build macOS App Bundle

```bash
bash build.sh
```

**Output:**

* `dist/PolymarketBot.app`
* `dist/PolymarketBot.dmg`

**Optional dependency:**

```bash
brew install create-dmg
```

---

### 💻 CLI Mode

```bash
# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env

# Run bot (dry run by default)
python main.py
```

🌐 Dashboard auto-starts at:

```text
http://localhost:8080
```

---

## 🧰 Commands

```bash
python app.py                # Launch native macOS desktop app
python app.py --cli          # Run CLI mode from desktop entry point
python main.py               # Run CLI bot + dashboard
python main.py --scan        # Scan active BTC 15-min markets
python main.py --arb-check   # Run one-shot arbitrage check
```

---

## ⚙️ Configuration

All CLI settings live in `.env`.

| Variable               | Default | Description                              |
| ---------------------- | ------: | ---------------------------------------- |
| `PRIVATE_KEY`          |       — | Polygon wallet private key               |
| `FUNDER_ADDRESS`       |       — | Proxy wallet address                     |
| `SIGNATURE_TYPE`       |     `2` | `0`=EOA, `1`=MagicLink, `2`=Google/proxy |
| `DRY_RUN`              |  `true` | Safe simulation mode                     |
| `POLLING_INTERVAL`     |     `5` | Seconds between refreshes                |
| `AUTO_EXECUTE`         | `false` | Auto-execute arbitrage trades            |
| `MAX_POSITION_SIZE`    |   `100` | Max shares per execution                 |
| `ARB_COOLDOWN_SECONDS` |   `120` | Cooldown between same-market trades      |
| `ARB_MIN_PROFIT`       | `0.005` | Minimum profit ($)                       |
| `ARB_MIN_ROI_PCT`      |   `0.3` | Minimum ROI (%)                          |
| `USE_WEBSOCKET`        |  `true` | Enable live streaming                    |
| `SPIKE_THRESHOLD`      |  `0.15` | Max allowed jump before validation       |
| `BUY_YES_TRIGGER`      |     `0` | YES auto-buy threshold                   |
| `BUY_NO_TRIGGER`       |     `0` | NO auto-buy threshold                    |
| `DIRECTIONAL_BUY_SIZE` |    `50` | Shares per directional buy               |
| `MARKET_REST_SECONDS`  |     `0` | Delay after new market opens             |
| `TELEGRAM_BOT_TOKEN`   |       — | Telegram bot token                       |
| `TELEGRAM_CHAT_ID`     |       — | Telegram chat ID                         |
| `ARB_ENABLED`          |  `true` | Master switch for arbitrage detection    |

> 💡 Most settings can also be changed live from the dashboard.

---

## 💰 How Arbitrage Works

In a binary market, **YES + NO always settles to $1.00**.

If both sides can be bought for less than **$1 total after fees**, the trade is risk-free.

```text
profit = $1.00 - (YES_ask + NO_ask) - fees
```

### 🧠 Two-Layer Analysis

1. ⚡ **Quick Check** — 1-share profitability test
2. 📚 **Depth Analysis** — Walks the order book to compute VWAP and maximum profitable size

---

## 🛡️ Spike Filter Logic

Thin books and stale ticks can create fake moves (like sudden 99¢ prints).

The spike filter protects execution by:

1. 📌 Tracking the last accepted price
2. 🚨 Detecting jumps larger than `SPIKE_THRESHOLD`
3. 🔁 Validating suspicious moves against REST
4. ✅ Accepting only confirmed moves
5. 🔄 Resetting on each new 15-minute market

---

## 🏗️ Project Architecture

```text
app.py
├── app_config.py
├── updater.py
├── version.py
├── dashboard/setup.html

main.py
├── config.py
├── market_discovery.py
├── market_data.py
├── arbitrage.py
├── trading.py
├── bot_state.py
├── dashboard_server.py
├── dashboard/
├── trade_log.py
├── notifications.py
├── models.py
└── utils.py
```

---

## 🖥️ Desktop App vs CLI

### 🍎 `app.py` (Desktop)

Best for macOS users who want a polished native experience.

* 🪟 Native `pywebview` window
* 🔄 Background Flask server on port `8089`
* 🧵 Thread-managed bot lifecycle
* 📝 JSON config storage (`~/Library/Application Support/PolymarketBot/config.json`)
* 🔄 In-app update system: background download, progress bar, install & restart via detached script

**Desktop app API routes** (beyond shared dashboard routes):
`/setup`, `POST /api/setup/save`, `GET /api/config`, `POST /api/config`, `POST /api/bot/start`, `POST /api/bot/stop`, `GET /api/update-status`, `POST /api/update-channel`, `POST /api/update-check`, `POST /api/update-download`, `GET /api/update-download-progress`, `POST /api/update-install`, `POST /api/suppress-beta-warning`, `POST /api/uninstall`

### ⌨️ `main.py` (CLI)

Best for servers, VPS, Docker, or power users.

* 🧠 Full CLI control
* 🌐 Built-in web dashboard
* 🧵 Clean daemon thread support
* 🛑 Graceful shutdown via `stop_event`

---

## 🌐 External APIs

* 📡 **Gamma API** — Market metadata, search, tags
* 📘 **CLOB API** — Order books, pricing, fees, order placement
* ⚡ **CLOB WebSocket** — Real-time market data
* 🐍 **py-clob-client** — Official Python SDK for trading

---

## ⚠️ Disclaimer

> 📚 This project is intended for **educational and research purposes only**.
>
> 💹 Prediction markets involve **real financial risk**.
>
> 🧪 Always start in **DRY RUN mode** and only trade with funds you can afford to lose.

---

## ❤️ Contributing Ideas

Potential future upgrades:

* 📉 Better charting and candle views
* 🤖 ML-based directional bias
* ☁️ Docker deployment templates
* 📲 Mobile push notifications
* 📊 Advanced trade analytics

######## WARNING ########
!!!!! Prediction markets involve financial risk. Always start in DRY RUN mode and use funds you can afford to lose. !!!!!!
