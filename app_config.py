"""
Application-level configuration — JSON-based config stored in
~/Library/Application Support/PolymarketBot/ (macOS) or equivalent.

This module handles first-run detection, saving/loading config,
and bridging to the existing config.py module-level constants.
"""

import json
import os
import platform
import sys
from pathlib import Path


def _get_config_dir() -> Path:
    """Return the platform-appropriate config directory."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:  # Linux / other
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "PolymarketBot"


CONFIG_DIR = _get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"

# Fields that must NEVER be writable over the HTTP API, because they
# control where money comes from / goes to. These can only be set via
# the pywebview Python↔JS bridge (AppBridge.save_setup in app.py), which
# is unreachable from a browser tab or a CSRF attack.
SENSITIVE_FIELDS = frozenset({
    "private_key",
    "funder_address",
    "signature_type",
    "telegram_bot_token",
    "telegram_chat_id",
})


# Default values for all settings
DEFAULTS = {
    # Wallet
    "private_key": "",
    "funder_address": "",
    "signature_type": 2,

    # Telegram
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",

    # Bot behaviour
    "dry_run": True,
    "polling_interval": 5,
    "use_websocket": True,
    "spike_threshold": 0.15,
    "market_rest_seconds": 480,
    "btc_price_poll_seconds": 3.0,

    # Arbitrage
    "arb_enabled": True,
    "arb_min_profit": 0.005,
    "arb_min_roi_pct": 0.3,
    "auto_execute": False,
    "max_position_size": 100,
    "arb_cooldown_seconds": 120,

    # Risk management
    "max_budget": 1000,
    "max_concurrent_positions": 3,
    "max_loss_per_trade": 10,
    "max_daily_loss": 50,
    "stop_loss_enabled": False,
    "stop_loss_amount": 100,

    # Directional
    "buy_yes_trigger": 0.87,
    "buy_no_trigger": 0.87,
    "max_buy_price": 0.96,
    "directional_buy_size": 50,

    # Update preferences
    "suppress_beta_warning": False,
}


def is_first_run() -> bool:
    """Return True if no config file exists yet."""
    return not CONFIG_FILE.exists()


def ensure_dirs():
    """Create config and log directories if they don't exist.

    Directories are created with mode 0o700 so that other users on the
    system cannot enumerate or read the config (which stores the wallet
    private key until migrated to Keychain).
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # If the directories already existed with looser permissions, tighten them.
    try:
        os.chmod(CONFIG_DIR, 0o700)
        os.chmod(LOG_DIR, 0o700)
    except OSError:
        pass


def load_config() -> dict:
    """Load config from JSON file, filling in defaults for missing keys."""
    ensure_dirs()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        # Merge with defaults so new keys are always present
        merged = {**DEFAULTS, **saved}
        return merged
    return dict(DEFAULTS)


def save_config(cfg: dict):
    """Save config dict to JSON file with owner-only permissions.

    The config file holds the wallet private key and Telegram bot token,
    so we atomically create a 0o600 file via ``os.open`` with
    ``O_CREAT | O_WRONLY | O_TRUNC`` and chmod existing files to 0o600
    for backwards compatibility.
    """
    ensure_dirs()
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    fd = os.open(CONFIG_FILE, flags, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    # If the file already existed with looser perms, ``os.open`` keeps
    # them. Force 0o600 afterwards.
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def apply_config_to_module(cfg: dict):
    """Push config values into the config.py module-level constants.

    This bridges the new JSON config into the existing codebase which
    reads config.PRIVATE_KEY, config.DRY_RUN, etc.
    """
    import config

    # snake_case JSON key -> UPPER_CASE config.py attribute.
    # DEFAULTS is the single source of truth for fallback values.
    _MIRRORED = (
        "private_key", "funder_address", "signature_type",
        "dry_run", "polling_interval", "use_websocket",
        "spike_threshold", "market_rest_seconds", "btc_price_poll_seconds",
        "arb_enabled", "arb_min_profit", "arb_min_roi_pct",
        "auto_execute", "max_position_size", "arb_cooldown_seconds",
        "max_budget", "max_concurrent_positions",
        "max_loss_per_trade", "max_daily_loss",
        "stop_loss_enabled", "stop_loss_amount",
        "buy_yes_trigger", "buy_no_trigger", "max_buy_price",
        "directional_buy_size",
        "telegram_bot_token", "telegram_chat_id",
    )
    for key in _MIRRORED:
        setattr(config, key.upper(), cfg.get(key, DEFAULTS[key]))
    config.BTC_PRICE_POLL_SECONDS = float(config.BTC_PRICE_POLL_SECONDS)
    config.TELEGRAM_ENABLED = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def get_config_for_api() -> dict:
    """Return a sanitized config dict for the frontend (no private key)."""
    cfg = load_config()
    safe = dict(cfg)
    # Mask private key — only show if it's set
    safe["private_key_set"] = bool(safe.get("private_key"))
    safe.pop("private_key", None)
    return safe


def update_config_from_api(updates: dict) -> dict:
    """Apply partial updates from the HTTP API, save, and re-apply.

    Wallet-critical fields (``SENSITIVE_FIELDS``) are silently dropped —
    they must be set via the AppBridge Python↔JS channel instead so
    they're not reachable over HTTP (even from localhost CSRF).
    """
    cfg = load_config()
    for key, value in updates.items():
        if key in DEFAULTS and key not in SENSITIVE_FIELDS:
            cfg[key] = value
    save_config(cfg)
    apply_config_to_module(cfg)
    return cfg


def save_setup_from_bridge(cfg: dict) -> None:
    """Persist a full config dict from the pywebview bridge.

    This IS allowed to write sensitive fields because the bridge is only
    reachable from JavaScript running inside the pywebview window, not
    from HTTP. Callers (e.g. ``AppBridge.save_setup``) should have
    already merged ``cfg`` with ``DEFAULTS``.
    """
    save_config(cfg)
    apply_config_to_module(cfg)
