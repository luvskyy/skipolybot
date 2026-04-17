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
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_from_directory

from bot_state import state

# Support PyInstaller bundle: _MEIPASS is the temp extraction dir
_BASE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
DASHBOARD_DIR = _BASE_DIR / "dashboard"
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))

# "cli" for the headless/Docker path, "desktop" for the pywebview app.
# app.py sets this to "desktop" before starting the server; Docker sets it via env.
RUNTIME_MODE = os.getenv("POLYBOT_RUNTIME_MODE", "cli").lower()

# Opt-in: allow non-loopback clients (e.g. host browser → container port map).
# The per-request Origin/Referer CSRF check still runs, so state-changing calls
# must come from a browser pointed at localhost.
ALLOW_REMOTE_DASHBOARD = os.getenv("ALLOW_REMOTE_DASHBOARD", "").lower() in ("1", "true", "yes")

# Only loopback clients are ever allowed to reach the Flask server — unless
# ALLOW_REMOTE_DASHBOARD is set (Docker path). Combined with the bind choice in
# ``start_dashboard`` this is belt-and-braces for the default local build.
_LOOPBACK_ADDRS = {"127.0.0.1", "::1"}

app = Flask(__name__, static_folder=str(DASHBOARD_DIR))


def _origin_host_is_loopback(value: str) -> bool:
    """Return True if an Origin/Referer URL points at loopback.

    Accepts any port because the CLI and desktop builds use different
    ports (8080 vs 8089) and future builds may change again.
    """
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


@app.before_request
def _enforce_loopback_and_origin():
    """Reject off-host clients and CSRF/cross-origin state changes.

    - GET/HEAD/OPTIONS are allowed from loopback without an Origin check
      because they should never have side effects.
    - Any state-changing method (POST/PUT/PATCH/DELETE) must carry an
      Origin (or Referer fallback) that points at loopback.
    """
    remote = request.remote_addr or ""
    if remote not in _LOOPBACK_ADDRS and not ALLOW_REMOTE_DASHBOARD:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if _origin_host_is_loopback(origin) or _origin_host_is_loopback(referer):
        return None
    return jsonify({"ok": False, "error": "cross-origin request blocked"}), 403

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


@app.route("/api/runtime-mode")
def api_runtime_mode():
    """Tell the dashboard whether desktop-only features (updater, uninstall) apply."""
    return jsonify({"mode": RUNTIME_MODE})


@app.route("/api/health")
def api_health():
    """Liveness probe: 200 only if the bot loop ticked within the last 60s."""
    import time as _time
    last = state.last_tick
    age = (_time.time() - last) if last else None
    if age is None or age > 60:
        return jsonify({"status": "stale", "last_tick_age": age}), 503
    return jsonify({"status": "ok", "last_tick_age": age})


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


@app.route("/api/trade/<int:trade_id>")
def api_trade_detail(trade_id):
    """Full detail for a single trade including price history."""
    detail = state.get_trade_detail(trade_id)
    if detail is None:
        return jsonify({"error": "Trade not found"}), 404
    return jsonify(detail)


@app.route("/api/logs/export")
def api_logs_export():
    """Export all buffered bot logs as a downloadable plain-text file."""
    lines = state.get_all_logs()
    body = "\n".join(lines) + ("\n" if lines else "")
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    filename = f"polybot-logs-{ts}.txt"
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/logs")
def api_logs_json():
    """Return all buffered logs as JSON (for preview/copy)."""
    return jsonify({"logs": state.get_all_logs()})


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

    # When remote access is enabled (Docker), bind all interfaces so the host's
    # port map reaches us. Otherwise stay on loopback for the local .app build.
    host = "0.0.0.0" if ALLOW_REMOTE_DASHBOARD else "127.0.0.1"
    banner = f"\n  Dashboard: http://localhost:{port}\n"
    if ALLOW_REMOTE_DASHBOARD:
        banner += (
            "  [warn] ALLOW_REMOTE_DASHBOARD=true — bound on 0.0.0.0, no auth.\n"
            "         Only expose this port to trusted networks.\n"
        )

    if blocking:
        print(banner)
        app.run(host=host, port=port, debug=False, use_reloader=False)
    else:
        t = threading.Thread(
            target=lambda: app.run(
                host=host, port=port, debug=False, use_reloader=False
            ),
            daemon=True,
        )
        t.start()
        print(banner)
        return t


if __name__ == "__main__":
    start_dashboard(blocking=True)
