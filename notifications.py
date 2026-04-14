"""
Telegram notifications & command handler.

Uses the Telegram Bot API directly via requests (no extra dependencies).
Disabled when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is unset.

Outbound: send alerts for arb opportunities, executions, and market events.
Inbound:  poll for commands (/test, /status) via getUpdates long-polling.
"""

import threading
import time

import requests

import config
from models import ArbitrageOpportunity
from utils import log


_API_BASE = "https://api.telegram.org/bot{token}"


def _send(text: str, parse_mode: str = "HTML"):
    """Send a message to the configured Telegram chat. Non-blocking."""
    if not config.TELEGRAM_ENABLED:
        return

    def _do_send():
        try:
            url = f"{_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN)}/sendMessage"
            resp = requests.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not resp.ok:
                log.debug(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            log.debug(f"Telegram send error: {e}")

    threading.Thread(target=_do_send, daemon=True).start()


def _send_sync(chat_id: str | int, text: str, parse_mode: str = "HTML"):
    """Send a message to a specific chat (synchronous, used by command handler)."""
    try:
        url = f"{_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN)}/sendMessage"
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not resp.ok:
            log.debug(f"Telegram reply failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log.debug(f"Telegram reply error: {e}")


# ── Command Handler ───────────────────────────────────────────────────────────

def _handle_command(chat_id: str | int, command: str):
    """Process an incoming Telegram command and reply."""
    cmd = command.strip().split()[0].lower().split("@")[0]  # strip bot username suffix

    if cmd == "/test":
        mode = "DRY RUN" if config.DRY_RUN else "LIVE"
        _send_sync(chat_id, f"✅ <b>PolyBot is connected!</b>\n\nMode: {mode}")

    elif cmd == "/status":
        from bot_state import state
        snap = state.snapshot()

        mode = "DRY RUN" if config.DRY_RUN else "LIVE"
        auto = "ON" if config.AUTO_EXECUTE else "OFF"
        ws = "ON" if config.USE_WEBSOCKET else "OFF"
        running = "Running" if snap.get("running") else "Stopped"
        cycle = snap.get("cycle", 0)

        market_q = "--"
        market_data = snap.get("market")
        if market_data and market_data.get("question"):
            market_q = _esc(market_data["question"][:60])

        arb_line = "No signal"
        arb_data = snap.get("arb")
        if arb_data and arb_data.get("is_profitable"):
            arb_line = (
                f"Profit: ${arb_data.get('net_profit', 0):.4f} | "
                f"ROI: {arb_data.get('roi_pct', 0):.2f}%"
            )

        pnl = snap.get("pnl", {})
        total_pnl = pnl.get("total_pnl", 0)
        total_trades = pnl.get("total_trades", 0)

        text = (
            f"<b>📊 PolyBot Status</b>\n\n"
            f"<b>State:</b> {running}\n"
            f"<b>Mode:</b> {mode}\n"
            f"<b>Cycle:</b> #{cycle}\n"
            f"<b>Auto-execute:</b> {auto}\n"
            f"<b>WebSocket:</b> {ws}\n\n"
            f"<b>Market:</b> {market_q}\n"
            f"<b>Arb:</b> {arb_line}\n\n"
            f"<b>Total PnL:</b> ${total_pnl:.4f}\n"
            f"<b>Trades:</b> {total_trades}"
        )
        _send_sync(chat_id, text)

    elif cmd == "/help":
        text = (
            "<b>🤖 PolyBot Commands</b>\n\n"
            "/test — Check bot connectivity\n"
            "/status — Current bot status & market info\n"
            "/help — Show this help"
        )
        _send_sync(chat_id, text)

    else:
        _send_sync(chat_id, f"Unknown command: <code>{_esc(cmd)}</code>\nTry /help")


def _command_poller():
    """Background thread that polls Telegram for incoming commands."""
    offset = 0
    base_url = _API_BASE.format(token=config.TELEGRAM_BOT_TOKEN)
    allowed_chat = str(config.TELEGRAM_CHAT_ID)

    while True:
        try:
            resp = requests.get(
                f"{base_url}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": '["message"]'},
                timeout=35,
            )
            if not resp.ok:
                log.debug(f"Telegram getUpdates failed: {resp.status_code}")
                time.sleep(5)
                continue

            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                # Only respond to commands from the configured chat
                if chat_id != allowed_chat:
                    continue
                if not text.startswith("/"):
                    continue

                log.debug(f"Telegram command received: {text}")
                _handle_command(chat_id, text)

        except requests.exceptions.ReadTimeout:
            continue  # normal for long-polling
        except Exception as e:
            log.debug(f"Telegram poller error: {e}")
            time.sleep(5)


def start_command_listener():
    """Start the Telegram command listener in a background thread."""
    if not config.TELEGRAM_ENABLED:
        return
    t = threading.Thread(target=_command_poller, daemon=True, name="telegram-cmds")
    t.start()
    log.info("Telegram command listener started")


def notify_arb_detected(opp: ArbitrageOpportunity):
    """Alert when a profitable arbitrage opportunity is detected."""
    text = (
        f"<b>🟢 Arbitrage Detected</b>\n"
        f"<b>Market:</b> {_esc(opp.market_question)}\n"
        f"<b>YES ask:</b> ${opp.yes_price:.4f}  |  <b>NO ask:</b> ${opp.no_price:.4f}\n"
        f"<b>Combined:</b> ${opp.combined_cost:.4f}\n"
        f"<b>Net profit:</b> ${opp.net_profit:.4f}  |  <b>ROI:</b> {opp.roi_pct:.2f}%\n"
        f"<b>Fees:</b> ${opp.total_fees:.4f} ({opp.fee_rate_bps} bps)\n"
        f"<b>Max size:</b> {opp.max_profitable_size:.0f} shares"
    )
    _send(text)


def notify_execution(
    market_question: str,
    size: float,
    yes_price: float,
    no_price: float,
    net_profit: float,
    roi_pct: float,
    status: str,
    dry_run: bool = False,
):
    """Alert when a trade is executed (or would be, in dry run)."""
    mode = " [DRY RUN]" if dry_run else ""
    icon = {"SUCCESS": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}.get(status, "❓")
    text = (
        f"<b>{icon} Trade Executed{mode}</b>\n"
        f"<b>Market:</b> {_esc(market_question)}\n"
        f"<b>Size:</b> {size:.0f} shares\n"
        f"<b>YES:</b> ${yes_price:.4f}  |  <b>NO:</b> ${no_price:.4f}\n"
        f"<b>Expected profit:</b> ${net_profit:.4f} ({roi_pct:.2f}%)\n"
        f"<b>Status:</b> {status}"
    )
    _send(text)


def notify_market_switch(old_question: str | None, new_question: str, time_remaining: float | None):
    """Alert when the bot switches to a new market."""
    remaining = f"{time_remaining / 60:.0f}m" if time_remaining else "unknown"
    old_part = f"\n<b>Previous:</b> {_esc(old_question)}" if old_question else ""
    text = (
        f"<b>🔄 Market Switch</b>\n"
        f"<b>New:</b> {_esc(new_question)}{old_part}\n"
        f"<b>Resolves in:</b> {remaining}"
    )
    _send(text)


def notify_startup():
    """Alert when the bot starts up."""
    mode = "DRY RUN" if config.DRY_RUN else "LIVE"
    auto = "ON" if config.AUTO_EXECUTE else "OFF"
    ws = "ON" if config.USE_WEBSOCKET else "OFF"
    text = (
        f"<b>🤖 Bot Started</b>\n"
        f"<b>Mode:</b> {mode}\n"
        f"<b>Auto-execute:</b> {auto} (max {config.MAX_POSITION_SIZE:.0f} shares)\n"
        f"<b>WebSocket:</b> {ws}\n"
        f"<b>Poll interval:</b> {config.POLLING_INTERVAL}s"
    )
    _send(text)


def notify_stop_loss(
    market_question: str,
    side: str,
    size: float,
    entry_price: float,
    exit_price: float,
    loss: float,
    dry_run: bool = False,
):
    """Alert when a stop-loss is triggered."""
    mode = " [DRY RUN]" if dry_run else ""
    text = (
        f"<b>🛑 Stop-Loss Triggered{mode}</b>\n"
        f"<b>Market:</b> {_esc(market_question)}\n"
        f"<b>Side:</b> {side.upper()}\n"
        f"<b>Size:</b> {size:.0f} shares\n"
        f"<b>Entry:</b> ${entry_price:.4f}  →  <b>Exit:</b> ${exit_price:.4f}\n"
        f"<b>Loss:</b> ${abs(loss):.2f}"
    )
    _send(text)


def notify_shutdown():
    """Alert when the bot shuts down."""
    _send("<b>🛑 Bot Stopped</b>")


def _esc(text: str | None) -> str:
    """Escape HTML special characters for Telegram."""
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
