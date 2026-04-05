"""
Utility helpers — logging, time, formatting.
"""

import logging
import sys
from datetime import datetime, timezone


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure structured logging for the bot."""
    logger = logging.getLogger("polybot")
    if logger.handlers:
        return logger

    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    return logger


log = setup_logging()


# ── Time Helpers ─────────────────────────────────────────────────────────────

def current_utc() -> datetime:
    """Return current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def floor_to_15min(dt: datetime) -> datetime:
    """Round a datetime DOWN to the nearest 15-minute boundary."""
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def next_15min(dt: datetime) -> datetime:
    """Return the START of the next 15-minute window."""
    from datetime import timedelta
    floored = floor_to_15min(dt)
    return floored + timedelta(minutes=15)


def epoch_for_15min_window(dt: datetime) -> int:
    """Get the Unix epoch timestamp for the 15-minute window containing `dt`."""
    floored = floor_to_15min(dt)
    return int(floored.timestamp())


def format_countdown(seconds: float) -> str:
    """Format seconds into a human-readable countdown like '8m 23s'."""
    if seconds <= 0:
        return "RESOLVED"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_price(price: float | None) -> str:
    """Format a price nicely, e.g. $0.54 or '--' if None."""
    if price is None:
        return " --  "
    return f"${price:.2f}"


def format_pct(value: float) -> str:
    """Format a percentage, e.g. 2.35%."""
    return f"{value:.2f}%"


def format_usd(value: float) -> str:
    """Format a dollar amount, e.g. $0.0053."""
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"
