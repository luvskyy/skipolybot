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

**Known settings gaps (as of v1.2.1-beta.3):**
- `ARB_ENABLED` is now fully wired: present in `config.py`, `app_config.DEFAULTS`, `apply_config_to_module`, `bot_state.get_settings()`, `app.js SETTINGS_DEFAULTS`, and `BOOL_FIELDS`. Fixed in session 2026-04-05 16:00.
- `SPIKE_THRESHOLD` is in `bot_state.set_settings` validators but missing from `get_settings()` and the frontend form — the validator is unreachable. Still open.

**Trade detail panel (v1.1.0):** `TradeRecord` carries 11 rich context fields. Per-trade price histories use two structures: `_trade_entry_history[tid]` (snapshot of global deque at trade time) and `_trade_price_histories[tid]` (live ticks after entry, capped at 360). `get_trade_detail()` returns both; the frontend chart combines them. Route: `GET /api/trade/<int:trade_id>` in `dashboard_server.py`.

**Update channel selector (v1.1.0):** `updater.py` supports `"stable"` (releases/latest) and `"beta"` (all releases). Channel state lives in `updater._channel`; `get_status()` returns it. Routes `POST /api/update-channel` and `POST /api/update-check` in `app.py`. Frontend polls `/api/update-status` to detect when a background check finishes (`data.checking == false`).

**In-app update flow (v1.2.0):** `updater.DownloadStatus` tracks download progress. `start_download()` streams the DMG to a temp dir. `get_download_status()` is polled by the frontend at 500ms. `install_and_restart()` mounts the DMG via `hdiutil`, writes a detached bash script (`start_new_session=True`) that copies the `.app` and relaunches, then calls `os._exit(0)` after 0.5s. Install only works in bundled mode (`sys.frozen`). `suppress_beta_warning` key in JSON config controls the beta channel warning visibility in the update banner. **Known issue:** `showUpdateBanner()` in `app.js` has no idempotency guard — calling it twice (automatic poll + manual check) stacks duplicate click listeners on the Download button.

**`_parse_version()` fix (v1.2.1-beta.1):** Stable appends `(0,)`, pre-release appends `(-1, N)` where N is the numeric suffix. Fixes `1.2.0 > 1.2.0-beta.2` and `beta.2 > beta.1` comparisons that were broken by Python's shorter-tuple ordering.

**Winning-side resolution heuristic:** `main.py` infers `winning_side` from last prices (>0.85 bid = winner) before calling `resolve_trades()`. Markets resolving with both bids below 0.85 fall through to unrealized PnL fallback.

**Dashboard port:** `dashboard_server.py` default is 8080; `app.py` always passes 8089 explicitly.

**GitHub repo:** `https://github.com/luvskyy/skipolybot`. Current version: `1.2.1-beta.3`. Latest stable: `1.2.0`.

**README/CLAUDE.md status (as of 2026-04-06 session 00:12):** Both docs updated to cover in-app update system, new API endpoints, `suppress_beta_warning` config key, corrected `VERSION` constant reference (now 1.2.1-beta.3), and `bot_state.update_trade_pnl` None-guard behavior.

**Stop-loss PnL bug (fixed in v1.2.1-beta.3):** `update_trade_pnl` previously coerced `None` bids to $0, making unrealized loss equal to full cost. `main.py` stop-loss then passed that inflated unrealized PnL directly as `realized_loss`. Both fixed: `update_trade_pnl` now skips on `None`; stop-loss computes `realized_loss = (exit_price - entry_price) * size`. **Remaining gap:** `stop_loss_trade()` does not update `session_pnl`, so stopped trades' losses are missing from the session summary.

**Why:** Understanding these cross-file contracts is essential for accurate integrity checks across sessions.
**How to apply:** When reviewing settings-related changes, always trace the full pipeline: `config.py` → `app_config.py` → `bot_state.get_settings/set_settings` → `app.js SETTINGS_DEFAULTS/INT_FIELDS/BOOL_FIELDS` → HTML form.
