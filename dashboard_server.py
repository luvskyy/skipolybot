"""
Dashboard web server — serves the monitoring UI and provides real-time data via SSE.

Run standalone:
    python dashboard_server.py

Or import and call start_dashboard() from main.py to run alongside the bot.
"""

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from bot_state import state

# Support PyInstaller bundle: _MEIPASS is the temp extraction dir
_BASE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
DASHBOARD_DIR = _BASE_DIR / "dashboard"
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))

app = Flask(__name__, static_folder=str(DASHBOARD_DIR))

# SSE subscriber management
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()


def _broadcast(data: dict):
    """Push a JSON event to all SSE subscribers."""
    msg = f"data: {json.dumps(data)}\n\n"
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _sse_publisher():
    """Background thread that watches bot_state and broadcasts changes."""
    last_rev = -1
    while True:
        try:
            current_rev = state.revision
            if current_rev != last_rev:
                last_rev = current_rev
                _broadcast(state.snapshot())
            time.sleep(0.25)
        except Exception:
            time.sleep(1)


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(DASHBOARD_DIR), "index.html")


@app.route("/assets/<path:filename>")
def static_assets(filename):
    return send_from_directory(str(DASHBOARD_DIR / "assets"), filename)


@app.route("/api/state")
def api_state():
    """Full state snapshot (for initial load)."""
    return jsonify(state.snapshot())


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Return current bot settings."""
    return jsonify(state.get_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    """Update bot settings at runtime."""
    body = request.get_json(silent=True)
    if not body or not isinstance(body, dict):
        return jsonify({"ok": False, "errors": ["Request body must be a JSON object"]}), 400
    result = state.set_settings(body)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events stream of state updates."""
    def generate():
        q = queue.Queue(maxsize=50)
        with _subscribers_lock:
            _subscribers.append(q)
        try:
            # Send initial state immediately
            yield f"data: {json.dumps(state.snapshot())}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    # Send keepalive comment
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Server Lifecycle ───────────────────────────────────────────────────────

def start_dashboard(port: int = DASHBOARD_PORT, blocking: bool = False):
    """Start the dashboard server, optionally in a background thread."""
    # Start the SSE publisher thread
    pub = threading.Thread(target=_sse_publisher, daemon=True)
    pub.start()

    if blocking:
        print(f"\n  Dashboard: http://localhost:{port}\n")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    else:
        t = threading.Thread(
            target=lambda: app.run(
                host="0.0.0.0", port=port, debug=False, use_reloader=False
            ),
            daemon=True,
        )
        t.start()
        print(f"\n  Dashboard: http://localhost:{port}\n")
        return t


if __name__ == "__main__":
    start_dashboard(blocking=True)
