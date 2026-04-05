---
name: Polymarket Bot Architecture Overview
description: Key architectural facts about this project that shape devlog and integrity review work
type: project
---

This is a Python bot targeting Polymarket 15-minute BTC Up/Down markets. Key structural facts:

- `config.py` is the single source of truth for all runtime settings; `bot_state.py` mirrors a subset and exposes a live-update API (`set_settings`) used by the web dashboard.
- `bot_state.py` acts as the in-memory bridge between the polling loop (`main.py`) and the web dashboard (`dashboard_server.py` + `dashboard/`). All cross-thread communication goes through `BotState` methods.
- The web dashboard uses Server-Sent Events (SSE) for real-time updates, with `app.js` as a pure client — no framework.
- `app.py` is the GUI entry point (pywebview + Flask). It starts Flask on port 8089, opens a native macOS window, and runs the bot in a daemon thread. `main.py` remains the CLI entry point. The `--cli` flag in `app.py` delegates back to `main.main()`.
- `app_config.py` manages JSON config at `~/Library/Application Support/PolymarketBot/config.json`. `apply_config_to_module()` bridges it to `config.py` module constants. All callers pass a DEFAULTS-merged dict so the `apply_config_to_module` fallbacks are dormant but should match `DEFAULTS` for safety.

**Settings pipeline to always cross-check:** `config.py` constant → `app_config.py DEFAULTS` + `apply_config_to_module` → `bot_state.get_settings()` → `app.js SETTINGS_DEFAULTS` + `INT_FIELDS`/`BOOL_FIELDS` → HTML settings form `name` attribute.

**Known settings gaps (as of v1.1.0):**
- `ARB_ENABLED` exists in `config.py`, `app_config.DEFAULTS`, and `apply_config_to_module`, and correctly gates arb detection in `main.py:396`, but is missing from `bot_state.get_settings()` and the frontend settings form — cannot be toggled from the dashboard.
- `SPIKE_THRESHOLD` is in `bot_state.set_settings` validators but missing from `get_settings()` and the frontend form — the validator is unreachable.
- `get_channel` is imported in `app.py` but never called — dead import.

**Trade detail panel (v1.1.0):** `TradeRecord` carries 11 rich context fields. Per-trade price histories use two structures: `_trade_entry_history[tid]` (snapshot of global deque at trade time) and `_trade_price_histories[tid]` (live ticks after entry, capped at 360). `get_trade_detail()` returns both; the frontend chart combines them. Route: `GET /api/trade/<int:trade_id>` in `dashboard_server.py`.

**Update channel selector (v1.1.0):** `updater.py` supports `"stable"` (releases/latest) and `"beta"` (all releases). Channel state lives in `updater._channel`; `get_status()` returns it. Routes `POST /api/update-channel` and `POST /api/update-check` in `app.py`. Frontend polls `/api/update-status` to detect when a background check finishes (`data.checking == false`).

**Winning-side resolution heuristic:** `main.py` infers `winning_side` from last prices (>0.85 bid = winner) before calling `resolve_trades()`. Markets resolving with both bids below 0.85 fall through to unrealized PnL fallback.

**Dashboard port:** `dashboard_server.py` default is 8080; `app.py` always passes 8089 explicitly. The default only applies if `dashboard_server.py` is run directly.

**GitHub repo:** `https://github.com/luvskyy/skipolybot`. Current version: 1.1.0. Auto-update checker polls the GitHub Releases API; `GITHUB_ALL_RELEASES_URL` added to `version.py` for beta channel checks.

**README/CLAUDE.md status (as of 2026-04-05):** Both docs updated to cover desktop app, updater, trade detail panel, ARB_ENABLED, and current architecture. No longer out of date.

**Why:** Understanding these cross-file contracts is essential for accurate integrity checks across sessions.
**How to apply:** When reviewing settings-related changes, always trace the full pipeline: `config.py` → `app_config.py` → `bot_state.get_settings/set_settings` → `app.js SETTINGS_DEFAULTS/INT_FIELDS/BOOL_FIELDS` → HTML form.
