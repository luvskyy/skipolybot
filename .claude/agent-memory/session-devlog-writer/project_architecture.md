---
name: Polymarket Bot Architecture Overview
description: Key architectural facts about this project that shape devlog and integrity review work
type: project
---

This is a Python bot targeting Polymarket 15-minute BTC Up/Down markets. Key structural facts:

- `config.py` is the single source of truth for all runtime settings; `bot_state.py` mirrors a subset and exposes a live-update API (`set_settings`) used by the web dashboard.
- `bot_state.py` acts as the in-memory bridge between the polling loop (`main.py`) and the web dashboard (`dashboard_server.py` + `dashboard/`). All cross-thread communication goes through `BotState` methods.
- The web dashboard uses Server-Sent Events (SSE) for real-time updates, with `app.js` as a pure client — no framework.
- `POLLING_INTERVAL` is a float in `config.py` and `bot_state.py` but was incorrectly placed in `INT_FIELDS` in `app.js`, which causes `parseInt` truncation on form save. This is a known open bug as of 2026-04-03.
- `resolve_trades()` in `bot_state.py` accepts a `winning_side` argument but `main.py` never passes it — directional trades always resolve via unrealized PnL fallback rather than actual outcome.
- `drawLine()` in `app.js` is dead code — it was superseded by `drawSeries()` and is never called.

**Why:** Understanding these cross-file contracts is essential for accurate integrity checks across sessions.
**How to apply:** When reviewing settings-related changes, always cross-check type handling in `config.py` (source), `bot_state.py` validators, and `app.js` `INT_FIELDS`/`BOOL_FIELDS` sets.
