---
name: Polymarket Bot Architecture Overview
description: Key architectural facts about this project that shape devlog and integrity review work
type: project
---

This is a Python bot targeting Polymarket 15-minute BTC Up/Down markets. Key structural facts:

- `config.py` is the single source of truth for all runtime settings; `bot_state.py` mirrors a subset and exposes a live-update API (`set_settings`) used by the web dashboard.
- `bot_state.py` acts as the in-memory bridge between the polling loop (`main.py`) and the web dashboard (`dashboard_server.py` + `dashboard/`). All cross-thread communication goes through `BotState` methods.
- The web dashboard uses Server-Sent Events (SSE) for real-time updates, with `app.js` as a pure client — no framework.
- `app.py` is the new GUI entry point (pywebview + Flask). It starts Flask on port 8089, opens a native macOS window, and runs the bot in a daemon thread. `main.py` remains the CLI entry point. The `--cli` flag in `app.py` delegates back to `main.main()`.
- `app_config.py` manages JSON config at `~/Library/Application Support/PolymarketBot/config.json`. `apply_config_to_module()` bridges it to `config.py` module constants. All callers pass a DEFAULTS-merged dict so the `apply_config_to_module` fallbacks are dormant but should match `DEFAULTS` for safety.
- `DEFAULTS` in `app_config.py` contains `arb_enabled` but `config.py` has no `ARB_ENABLED` constant and `apply_config_to_module` never pushes it. The setting is saved but has no effect — open bug as of 2026-04-05.
- `apply_config_to_module` line 119: fallback for `market_rest_seconds` is `0` but `DEFAULTS` says `480`. Inconsistency is dormant in normal flow but should be fixed.
- `POLLING_INTERVAL` is a float in `config.py` and `bot_state.py` but was incorrectly placed in `INT_FIELDS` in `app.js`, which causes `parseInt` truncation on form save. Known open bug as of 2026-04-03.
- `resolve_trades()` in `bot_state.py` accepts a `winning_side` argument but `main.py` never passes it — directional trades always resolve via unrealized PnL fallback rather than actual outcome.
- `drawLine()` in `app.js` is dead code — it was superseded by `drawSeries()` and is never called.
- Dashboard port: `dashboard_server.py` default is 8080; `app.py` always passes 8089 explicitly. The default only applies if `dashboard_server.py` is run directly.
- The GitHub repo is `https://github.com/luvskyy/skipolybot`. Auto-update checker (`updater.py`) polls the GitHub Releases API against `VERSION` in `version.py`.
- README was updated on 2026-04-05 to document `app.py` desktop launch, setup wizard, `build.sh`, and the desktop-vs-CLI architecture split. Previously README only described the CLI path.

**Why:** Understanding these cross-file contracts is essential for accurate integrity checks across sessions.
**How to apply:** When reviewing settings-related changes, always cross-check type handling in `config.py` (source), `bot_state.py` validators, `app_config.py` DEFAULTS, `apply_config_to_module`, and `app.js` `INT_FIELDS`/`BOOL_FIELDS` sets.
