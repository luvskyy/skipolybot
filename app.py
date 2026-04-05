"""
Desktop application entry point — wraps the Flask dashboard + bot
in a native pywebview window.

Launch:
    python app.py          # Normal GUI launch
    python app.py --cli    # Fall back to original CLI mode
"""

import argparse
import os
import signal
import sys
import threading
import time

import webview

from app_config import (
    is_first_run, load_config, save_config, apply_config_to_module,
    get_config_for_api, update_config_from_api, CONFIG_DIR, DEFAULTS,
)
from dashboard_server import app as flask_app, start_dashboard
from updater import get_status as get_update_status, start_update_check, check_for_update, set_channel, get_channel
from version import VERSION


# ── API bridge exposed to JavaScript ─────────────────────────────────────────

class AppBridge:
    """Python <-> JS bridge accessible via window.pywebview.api.*"""

    def is_first_run(self) -> bool:
        return is_first_run()

    def get_defaults(self) -> dict:
        return dict(DEFAULTS)

    def get_config(self) -> dict:
        return get_config_for_api()

    def save_setup(self, cfg: dict) -> dict:
        """Called by the setup wizard to save initial config."""
        full = {**DEFAULTS, **cfg}
        save_config(full)
        apply_config_to_module(full)
        return {"ok": True}

    def update_config(self, updates: dict) -> dict:
        """Partial config update from settings panel."""
        update_config_from_api(updates)
        return {"ok": True}

    def start_bot(self) -> dict:
        """Start the bot loop in a background thread."""
        _start_bot_thread()
        return {"ok": True}

    def stop_bot(self) -> dict:
        """Signal the bot to stop."""
        _stop_bot()
        return {"ok": True}

    def get_config_dir(self) -> str:
        return str(CONFIG_DIR)


# ── Bot thread management ────────────────────────────────────────────────────

_bot_thread: threading.Thread | None = None
_bot_stop_event = threading.Event()


def _start_bot_thread():
    global _bot_thread
    if _bot_thread and _bot_thread.is_alive():
        return  # already running

    _bot_stop_event.clear()

    def _run():
        # Import here to avoid circular imports and to pick up applied config
        from main import run_bot
        try:
            # enable_dashboard=False because Flask is already running from app.py
            run_bot(enable_dashboard=False, stop_event=_bot_stop_event)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Bot thread error: {e}")

    _bot_thread = threading.Thread(target=_run, daemon=True, name="bot-loop")
    _bot_thread.start()


def _stop_bot():
    _bot_stop_event.set()


# ── Setup wizard route ───────────────────────────────────────────────────────

@flask_app.route("/setup")
def setup_page():
    from flask import send_from_directory
    from dashboard_server import DASHBOARD_DIR
    return send_from_directory(str(DASHBOARD_DIR), "setup.html")


@flask_app.route("/api/setup/save", methods=["POST"])
def api_setup_save():
    from flask import request, jsonify
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"ok": False, "error": "No data"}), 400
    full = {**DEFAULTS, **body}
    save_config(full)
    apply_config_to_module(full)
    return jsonify({"ok": True})


@flask_app.route("/api/config", methods=["GET"])
def api_config_get():
    from flask import jsonify
    return jsonify(get_config_for_api())


@flask_app.route("/api/config", methods=["POST"])
def api_config_post():
    from flask import request, jsonify
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"ok": False, "error": "No data"}), 400
    update_config_from_api(body)
    return jsonify({"ok": True})


@flask_app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    from flask import jsonify
    _start_bot_thread()
    return jsonify({"ok": True})


@flask_app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    from flask import jsonify
    _stop_bot()
    return jsonify({"ok": True})


@flask_app.route("/api/update-status")
def api_update_status():
    from flask import jsonify
    return jsonify(get_update_status())


@flask_app.route("/api/update-channel", methods=["POST"])
def api_update_channel():
    """Switch update channel (stable/beta) and re-check."""
    from flask import request, jsonify
    body = request.get_json(silent=True)
    if not body or "channel" not in body:
        return jsonify({"ok": False, "error": "Missing channel"}), 400
    channel = body["channel"]
    if channel not in ("stable", "beta"):
        return jsonify({"ok": False, "error": "Invalid channel"}), 400
    set_channel(channel)
    # Re-check in background with the new channel
    import threading
    threading.Thread(target=check_for_update, daemon=True, name="update-recheck").start()
    return jsonify({"ok": True, "channel": channel})


@flask_app.route("/api/update-check", methods=["POST"])
def api_update_check():
    """Manually trigger an update check."""
    from flask import jsonify
    import threading
    threading.Thread(target=check_for_update, daemon=True, name="update-manual-check").start()
    return jsonify({"ok": True})


@flask_app.route("/api/uninstall", methods=["POST"])
def api_uninstall():
    """Remove all user data and move the app to Trash."""
    from flask import jsonify
    import shutil
    import subprocess

    _stop_bot()

    # 1. Delete config directory
    try:
        if CONFIG_DIR.exists():
            shutil.rmtree(CONFIG_DIR)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to remove config: {e}"}), 500

    # 2. Move .app to Trash via AppleScript (works even without permissions dialogs)
    #    Detect if we're running from a .app bundle or dev mode
    app_path = None
    if getattr(sys, 'frozen', False):
        # PyInstaller bundle — find the .app container
        exe_path = os.path.realpath(sys.executable)
        # exe is at PolymarketBot.app/Contents/MacOS/PolymarketBot
        parts = exe_path.split("/")
        for i, part in enumerate(parts):
            if part.endswith(".app"):
                app_path = "/".join(parts[:i + 1])
                break

    trash_result = None
    if app_path and os.path.exists(app_path):
        try:
            subprocess.run([
                "osascript", "-e",
                f'tell application "Finder" to delete POSIX file "{app_path}"'
            ], timeout=5, capture_output=True)
            trash_result = "moved_to_trash"
        except Exception:
            trash_result = "manual_delete_needed"
    else:
        trash_result = "dev_mode"

    # 3. Schedule quit after response is sent
    def _quit_later():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_quit_later, daemon=True).start()

    return jsonify({"ok": True, "trash": trash_result})


# ── Window lifecycle ─────────────────────────────────────────────────────────

def _on_closing():
    """Clean up when the window is closed."""
    _stop_bot()
    time.sleep(0.3)
    os._exit(0)


def main():
    parser = argparse.ArgumentParser(description="PolymarketBot Desktop")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode (no GUI)")
    args = parser.parse_args()

    if args.cli:
        # Fall back to original CLI
        import main as cli_main
        cli_main.main()
        return

    # Load config if it exists, apply to config module
    if not is_first_run():
        cfg = load_config()
        apply_config_to_module(cfg)

    # Start Flask in background
    start_dashboard(port=8089, blocking=False)
    time.sleep(0.3)  # let Flask bind

    # Check for updates in background
    start_update_check()

    # Determine start URL
    if is_first_run():
        start_url = "http://localhost:8089/setup"
    else:
        start_url = "http://localhost:8089"

    # Create native window
    bridge = AppBridge()
    window = webview.create_window(
        title="PolymarketBot",
        url=start_url,
        width=1280,
        height=820,
        min_size=(900, 600),
        js_api=bridge,
        confirm_close=False,
    )

    window.events.closing += _on_closing

    # Auto-start bot if config exists and not first run
    if not is_first_run():
        threading.Timer(1.0, _start_bot_thread).start()

    webview.start(debug=False)


if __name__ == "__main__":
    main()
