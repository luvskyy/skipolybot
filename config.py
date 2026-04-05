"""
Central configuration — loads .env and exposes all settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ── API Hosts ────────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
WS_HOST = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Blockchain ───────────────────────────────────────────────────────────────
CHAIN_ID = 137  # Polygon mainnet

# ── Wallet / Auth ────────────────────────────────────────────────────────────
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))  # 2 = Google/proxy wallet

# ── Bot Behaviour ────────────────────────────────────────────────────────────
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
POLLING_INTERVAL = float(os.getenv("POLLING_INTERVAL", "1"))

# ── Arbitrage Thresholds (override via env or set later in strategy) ────────
# Minimum net profit (in $) to flag an arb opportunity
ARB_MIN_PROFIT = float(os.getenv("ARB_MIN_PROFIT", "0.005"))
# Minimum ROI % to flag as worth taking
ARB_MIN_ROI_PCT = float(os.getenv("ARB_MIN_ROI_PCT", "0.3"))

# ── Auto-Execution ──────────────────────────────────────────────────────────
AUTO_EXECUTE = os.getenv("AUTO_EXECUTE", "false").lower() in ("true", "1", "yes")
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "100"))
ARB_COOLDOWN_SECONDS = int(os.getenv("ARB_COOLDOWN_SECONDS", "120"))

# ── Risk Management ─────────────────────────────────────────────────────────
MAX_BUDGET = float(os.getenv("MAX_BUDGET", "1000"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))
MAX_LOSS_PER_TRADE = float(os.getenv("MAX_LOSS_PER_TRADE", "10"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "50"))
STOP_LOSS_ENABLED = os.getenv("STOP_LOSS_ENABLED", "false").lower() in ("true", "1", "yes")
STOP_LOSS_AMOUNT = float(os.getenv("STOP_LOSS_AMOUNT", "100"))

# ── Directional Buy Triggers ───────────────────────────────────────────────
# Buy YES when its price hits this threshold (0.0–1.0, 0 = disabled)
BUY_YES_TRIGGER = float(os.getenv("BUY_YES_TRIGGER", "0"))
# Buy NO when its price hits this threshold (0.0–1.0, 0 = disabled)
BUY_NO_TRIGGER = float(os.getenv("BUY_NO_TRIGGER", "0"))
# Size in shares for directional buys
DIRECTIONAL_BUY_SIZE = float(os.getenv("DIRECTIONAL_BUY_SIZE", "50"))

# ── Market Rest Period ─────────────────────────────────────────────────────
# Seconds to wait after a new market opens before trading (0 = no rest)
MARKET_REST_SECONDS = int(os.getenv("MARKET_REST_SECONDS", "0"))

# ── Spike Filter ───────────────────────────────────────────────────────────
# Max allowed price jump between ticks (0.0–1.0). Moves larger than this
# trigger REST confirmation before the price is accepted.
SPIKE_THRESHOLD = float(os.getenv("SPIKE_THRESHOLD", "0.15"))

# ── WebSocket ───────────────────────────────────────────────────────────────
USE_WEBSOCKET = os.getenv("USE_WEBSOCKET", "true").lower() in ("true", "1", "yes")

# ── Telegram Notifications ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def validate():
    """Validate that critical config is set. Call on startup."""
    errors = []
    if not PRIVATE_KEY or PRIVATE_KEY == "your_polygon_private_key_here":
        errors.append("PRIVATE_KEY is not set in .env")
    if SIGNATURE_TYPE in (1, 2) and (not FUNDER_ADDRESS or FUNDER_ADDRESS.startswith("0xYour")):
        errors.append("FUNDER_ADDRESS is required for proxy wallets (signature type 1/2). "
                       "Find it at polymarket.com/settings")
    return errors
