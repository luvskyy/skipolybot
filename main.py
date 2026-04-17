"""
Main entry point — CLI runner with live terminal dashboard.

Usage:
    python main.py              # Run the bot (polling loop)
    python main.py --scan       # One-shot: scan for markets and exit
    python main.py --arb-check  # One-shot: check arbitrage on current market
"""

import argparse
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import config
from models import Market, ArbitrageOpportunity, PriceSnapshot
from market_discovery import search_btc_15min_markets, get_current_market
from market_data import fetch_price_snapshot, fetch_price_snapshot_hybrid, fetch_btc_price, fetch_pyth_btc_price, fetch_pyth_btc_price_at, fetch_polymarket_prices, get_books_for_market, MarketWebSocket, spike_filter
from arbitrage import detect_arbitrage, detect_arbitrage_with_depth, log_opportunity, find_max_profitable_size
from trading import TradingClient
from trade_log import log_arb_opportunity, log_execution
from notifications import notify_arb_detected, notify_execution, notify_market_switch, notify_startup, notify_shutdown, notify_stop_loss, start_command_listener
from utils import log, format_countdown, format_price, format_usd, format_pct, current_utc
from bot_state import state as dashboard_state, DashboardLogHandler, TradeRecord


# ── Terminal Dashboard ───────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
BG_RED = "\033[41m"
BG_GREEN = "\033[42m"

BOX_W = 62


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def box_top():
    return f"{CYAN}╔{'═' * BOX_W}╗{RESET}"


def box_mid():
    return f"{CYAN}╠{'═' * BOX_W}╣{RESET}"


def box_bot():
    return f"{CYAN}╚{'═' * BOX_W}╝{RESET}"


def box_line(text: str, align: str = "left"):
    # Strip ANSI for length calculation
    import re
    clean = re.sub(r"\033\[[0-9;]*m", "", text)
    pad = BOX_W - len(clean)
    if pad < 0:
        pad = 0
    if align == "center":
        left_pad = pad // 2
        right_pad = pad - left_pad
        return f"{CYAN}║{RESET}{' ' * left_pad}{text}{' ' * right_pad}{CYAN}║{RESET}"
    return f"{CYAN}║{RESET} {text}{' ' * (pad - 1)}{CYAN}║{RESET}"


def render_dashboard(
    market: Market | None,
    prices: PriceSnapshot | None,
    arb: ArbitrageOpportunity | None,
    trader: TradingClient,
    last_refresh_ago: float,
    cycle_count: int,
    ws: "MarketWebSocket | None" = None,
    last_exec_status: str | None = None,
):
    """Render the full terminal dashboard."""
    clear_screen()
    lines = []

    # ── Header ───────────────────────────────────────────────────────────
    mode_tag = f"{BG_RED}{WHITE} DRY RUN {RESET}" if config.DRY_RUN else f"{BG_GREEN}{WHITE}  LIVE   {RESET}"
    lines.append(box_top())
    lines.append(box_line(f"{BOLD}{WHITE}POLYMARKET BTC 15-MIN BOT{RESET}          {mode_tag}", "left"))
    lines.append(box_mid())

    # ── Market Info ──────────────────────────────────────────────────────
    if market:
        q = market.question[:48] if market.question else "Unknown"
        lines.append(box_line(f"{WHITE}Market:{RESET} {q}"))
        lines.append(box_line(f"{DIM}Cond ID: {market.condition_id[:40]}...{RESET}"))

        if market.time_remaining is not None:
            remaining = market.time_remaining
            if remaining > 0:
                countdown = format_countdown(remaining)
                urgency = GREEN if remaining > 300 else (YELLOW if remaining > 60 else RED)
                lines.append(box_line(f"Resolves in: {urgency}{BOLD}{countdown}{RESET}"))
            else:
                lines.append(box_line(f"Status: {RED}RESOLVED / EXPIRED{RESET}"))
        else:
            lines.append(box_line(f"End date: {DIM}unknown{RESET}"))
    else:
        lines.append(box_line(f"{YELLOW}⏳ Searching for active market...{RESET}"))

    lines.append(box_mid())

    # ── Prices ───────────────────────────────────────────────────────────
    if prices and prices.yes_ask is not None:
        lines.append(box_line(
            f"{GREEN}YES (Up)  {RESET}→  Bid: {format_price(prices.yes_bid)}  │  "
            f"Ask: {BOLD}{format_price(prices.yes_ask)}{RESET}"
        ))
        lines.append(box_line(
            f"{RED}NO  (Down){RESET}→  Bid: {format_price(prices.no_bid)}  │  "
            f"Ask: {BOLD}{format_price(prices.no_ask)}{RESET}"
        ))

        combined = prices.combined_ask
        if combined is not None:
            spread_val = 1.0 - combined
            spread_color = GREEN if spread_val > 0 else (YELLOW if spread_val == 0 else RED)
            lines.append(box_line(
                f"Combined Ask: {BOLD}{format_usd(combined)}{RESET}   "
                f"Spread: {spread_color}{BOLD}{format_usd(spread_val)}{RESET}"
            ))
    else:
        lines.append(box_line(f"{DIM}Waiting for price data...{RESET}"))

    lines.append(box_mid())

    # ── Arbitrage ────────────────────────────────────────────────────────
    if arb:
        if arb.is_profitable:
            lines.append(box_line(
                f"{BG_GREEN}{WHITE}{BOLD} 🟢 ARBITRAGE DETECTED {RESET}"
            ))
            lines.append(box_line(
                f"  Net profit/share: {GREEN}{BOLD}{format_usd(arb.net_profit)}{RESET}  "
                f"ROI: {GREEN}{BOLD}{format_pct(arb.roi_pct)}{RESET}"
            ))
            lines.append(box_line(
                f"  Fees: {format_usd(arb.total_fees)} "
                f"({arb.fee_rate_bps} bps)"
            ))
            if arb.max_profitable_size > 0:
                lines.append(box_line(
                    f"  Max size: {arb.max_profitable_size:.0f} shares"
                ))
            if last_exec_status:
                status_color = GREEN if last_exec_status == "SUCCESS" else (
                    YELLOW if last_exec_status == "PARTIAL" else RED)
                lines.append(box_line(
                    f"  Last exec: {status_color}{BOLD}{last_exec_status}{RESET}"
                ))
            if config.AUTO_EXECUTE:
                lines.append(box_line(
                    f"  {DIM}Auto-execute: ON (max {config.MAX_POSITION_SIZE:.0f} shares){RESET}"
                ))
        else:
            lines.append(box_line(f"{DIM}ARBITRAGE: None detected{RESET}"))
            if arb.fee_rate_bps > 0:
                lines.append(box_line(
                    f"{DIM}Fee rate: {arb.fee_rate_bps} bps │ "
                    f"Fees eat: {format_usd(arb.total_fees)}/share{RESET}"
                ))
    else:
        lines.append(box_line(f"{DIM}ARBITRAGE: Awaiting data...{RESET}"))

    lines.append(box_mid())

    # ── Status Bar ───────────────────────────────────────────────────────
    open_orders = len(trader.get_open_orders()) if not config.DRY_RUN else 0
    refresh_str = f"{last_refresh_ago:.0f}s ago" if last_refresh_ago < 60 else "..."
    lines.append(box_line(
        f"Open orders: {open_orders}  │  "
        f"Cycle: #{cycle_count}  │  "
        f"Refresh: {refresh_str}"
    ))
    ws_tag = ""
    if config.USE_WEBSOCKET and ws:
        ws_tag = f"WS: {GREEN}ON{RESET}  │  " if ws.is_connected else f"WS: {RED}OFF{RESET}  │  "
    lines.append(box_line(
        f"{DIM}{ws_tag}Poll: {config.POLLING_INTERVAL}s  │  "
        f"Ctrl+C to quit{RESET}"
    ))
    lines.append(box_bot())

    print("\n".join(lines))


# ── Bot Main Loop ────────────────────────────────────────────────────────────

def _try_directional_buy(
    side_key: str,
    price: float,
    trigger: float,
    token_id: str,
    current_market,
    prices,
    market_age: float,
    resting: bool,
    placed: set,
    directional_buys_placed: dict,
    cid: str,
    trader,
):
    """Log diagnostics and place a directional limit buy when all gates pass."""
    label = side_key.upper()
    emoji = "🟢" if side_key == "yes" else "🔴"

    if trigger <= 0:
        return
    if side_key in placed:
        log.debug(f"{label} SKIP: already placed this market")
        return
    if resting:
        return
    if price < trigger:
        log.debug(f"{label} HOLD: ask={price:.3f} < trigger={trigger:.3f}")
        return
    if config.MAX_BUY_PRICE > 0 and price > config.MAX_BUY_PRICE:
        log.info(
            f"{label} BLOCKED: ask={price:.3f} > "
            f"MAX_BUY_PRICE={config.MAX_BUY_PRICE:.3f}"
        )
        return

    buy_size = min(config.DIRECTIONAL_BUY_SIZE, config.MAX_POSITION_SIZE)
    log.info(
        f"{emoji} OPEN buy_{side_key} — {buy_size:.0f} shares @ ${price:.3f} "
        f"(trigger={trigger:.3f}, cost=${buy_size*price:.2f}, "
        f"market='{current_market.question[:40]}')"
    )
    resp = trader.place_limit_order(
        market=current_market,
        token_id=token_id,
        price=price,
        size=buy_size,
        side="BUY",
    )
    placed.add(side_key)
    directional_buys_placed[cid] = placed
    status = "SUCCESS" if resp else "FAILED"
    yes_field = price if side_key == "yes" else 0
    no_field = price if side_key == "no" else 0
    dashboard_state.add_trade(TradeRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        market_question=current_market.question,
        condition_id=cid,
        size=buy_size,
        yes_price=yes_field,
        no_price=no_field,
        net_profit=0,
        roi_pct=0,
        status=status,
        dry_run=config.DRY_RUN,
        trade_type=f"buy_{side_key}",
        cost=buy_size * price,
        side=side_key,
        time_remaining=current_market.time_remaining or 0,
        market_age=market_age,
        combined_ask_at_entry=prices.combined_ask or 0,
        fee_rate_bps=current_market.fee_rate_bps,
        gross_spread=1.0 - (prices.combined_ask or 1.0),
        yes_bid_at_entry=prices.yes_bid or 0,
        no_bid_at_entry=prices.no_bid or 0,
    ))
    notify_execution(
        market_question=current_market.question,
        size=buy_size,
        yes_price=yes_field,
        no_price=no_field,
        net_profit=0,
        roi_pct=0,
        status=f"{label} BUY {status}",
        dry_run=config.DRY_RUN,
    )


def run_bot(enable_dashboard: bool = True, stop_event: threading.Event | None = None):
    """Main bot loop — discover market, poll prices, detect arbitrage."""
    log.info("Starting Polymarket BTC 15-min bot...")

    # ── Dashboard integration ───────────────────────────────────────────
    # Attach log handler so dashboard gets log lines
    dash_handler = DashboardLogHandler(dashboard_state)
    dash_handler.setFormatter(log.handlers[0].formatter if log.handlers else None)
    log.addHandler(dash_handler)

    # Push initial config to dashboard state
    dashboard_state.set_config(
        dry_run=config.DRY_RUN,
        auto_execute=config.AUTO_EXECUTE,
        use_websocket=config.USE_WEBSOCKET,
        polling_interval=config.POLLING_INTERVAL,
    )
    dashboard_state.set_running(True)

    # Start web dashboard server
    if enable_dashboard:
        from dashboard_server import start_dashboard
        start_dashboard(blocking=False)

    # Validate config
    errors = config.validate()
    if errors and not config.DRY_RUN:
        for e in errors:
            log.error(f"Config error: {e}")
        raise RuntimeError(f"Config validation failed: {'; '.join(errors)}")
    elif errors:
        log.warning("=" * 64)
        log.warning("DRY RUN active with incomplete config — trading disabled.")
        log.warning("Set credentials in .env (or dashboard) and restart to enable:")
        for e in errors:
            log.warning(f"  - {e}")
        log.warning("=" * 64)

    # Initialize trading client
    trader = TradingClient()
    trader.initialize()

    notify_startup()
    start_command_listener()

    # Initialize WebSocket (created once we have a market)
    market_ws: MarketWebSocket | None = None

    # Graceful shutdown
    running = True

    _sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal running, _sigint_count
        _sigint_count += 1
        running = False
        if stop_event:
            stop_event.set()
        if _sigint_count >= 2:
            print(f"\n{YELLOW}Force quit.{RESET}")
            os._exit(1)
        print(f"\n{YELLOW}Shutting down... (Ctrl+C again to force quit){RESET}")

    # signal handlers only work from the main thread
    import threading as _threading
    if _threading.current_thread() is _threading.main_thread():
        signal.signal(signal.SIGINT, handle_sigint)

    current_market: Market | None = None
    prices: PriceSnapshot | None = None
    arb: ArbitrageOpportunity | None = None
    cycle_count = 0
    last_refresh = 0.0

    # Auto-execution state
    last_exec_time: dict[str, float] = {}  # condition_id -> timestamp
    last_exec_status: str | None = None

    # Market rest state — track when each market was first seen
    market_first_seen: dict[str, float] = {}  # condition_id -> timestamp

    # Directional buy state — track buys already placed per market
    directional_buys_placed: dict[str, set] = {}  # condition_id -> {"yes","no"}

    # Stop-loss debounce — require 2 consecutive ticks below threshold before firing
    stop_loss_breach_count: dict[int, int] = {}  # trade_id -> consecutive breaches

    # Pre-rotation state
    next_market: Market | None = None
    prefetch_thread: threading.Thread | None = None
    prefetch_lock = threading.Lock()

    def _prefetch_next_market():
        nonlocal next_market
        try:
            candidate = get_current_market()
            if candidate and (current_market is None or
                              candidate.condition_id != current_market.condition_id):
                with prefetch_lock:
                    next_market = candidate
                log.info(f"Pre-fetched next market: {candidate.question}")
            else:
                log.debug("Pre-fetch: no different market found yet")
        except Exception as e:
            log.debug(f"Pre-fetch failed: {e}")

    while running and not (stop_event and stop_event.is_set()):
        cycle_count += 1
        dashboard_state.set_cycle(cycle_count)

        try:
            # ── Step 1: Discover / refresh market ────────────────────────
            if current_market is None or current_market.is_expired:
                # Resolve any open trades for the expiring market
                if current_market and current_market.is_expired:
                    # Fetch fresh unfiltered prices — end-of-market convergence
                    # to $1/$0 is expected, NOT a spike to reject
                    resolution_prices = fetch_price_snapshot_hybrid(
                        current_market, market_ws, skip_spike_filter=True,
                    )
                    winning_side = None
                    if (resolution_prices
                            and resolution_prices.yes_bid is not None
                            and resolution_prices.no_bid is not None):
                        if resolution_prices.yes_bid > config.WIN_DETECT_THRESHOLD:
                            winning_side = "yes"
                        elif resolution_prices.no_bid > config.WIN_DETECT_THRESHOLD:
                            winning_side = "no"
                    # Update state with true resolution prices before resolving
                    dashboard_state.set_prices(resolution_prices)
                    dashboard_state.update_trade_pnl(resolution_prices)
                    # Snapshot trades for this market BEFORE resolve (to log outcomes)
                    pre_resolve = [t for t in dashboard_state.get_open_trades()
                                   if t.get("condition_id") == current_market.condition_id]
                    dashboard_state.resolve_trades(current_market.condition_id, winning_side=winning_side)
                    dashboard_state.increment_market_cycle()
                    side_str = winning_side or "unknown"
                    log.info(f"🏁 CLOSE market — winner={side_str} question='{current_market.question[:50]}'")
                    # Log each trade's resolution
                    for pre in pre_resolve:
                        tid = pre.get("trade_id")
                        # Re-read fresh resolved state
                        resolved = dashboard_state.get_trade_detail(tid) if tid else None
                        if resolved:
                            pnl = resolved.get("net_profit", 0)
                            status = resolved.get("status", "?")
                            emoji = "🏆" if pnl > 0 else ("💀" if pnl < 0 else "➖")
                            outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAK-EVEN")
                            log.info(
                                f"{emoji} {outcome} trade #{tid} — "
                                f"{resolved.get('trade_type')}/{resolved.get('side','')} "
                                f"size={resolved.get('size',0):.0f} "
                                f"pnl=${pnl:.2f} status={status}"
                            )
                old_question = current_market.question if current_market else None
                with prefetch_lock:
                    if next_market is not None:
                        log.info(f"Switching to pre-fetched market: {next_market.question}")
                        current_market = next_market
                        next_market = None
                    else:
                        log.info("Discovering 15-min BTC market...")
                        current_market = get_current_market()

                if current_market:
                    # Reset spike filter for new market (new price baseline)
                    spike_filter.reset()

                    # Track when we first see this market (for rest period)
                    cid = current_market.condition_id
                    if cid not in market_first_seen:
                        market_first_seen[cid] = time.time()
                        directional_buys_placed[cid] = set()
                    log.info(f"Found: {current_market.question}")
                    notify_market_switch(old_question, current_market.question, current_market.time_remaining)
                    log.info(f"  YES token: {current_market.yes_token_id[:30]}...")
                    log.info(f"  NO  token: {current_market.no_token_id[:30]}...")
                    if current_market.end_date:
                        log.info(f"  Ends: {current_market.end_date.isoformat()}")

                    # Fetch oracle "price to beat" from Polymarket page
                    pm_prices = fetch_polymarket_prices(current_market.slug)
                    if pm_prices.get("open_price"):
                        current_market.strike_price = pm_prices["open_price"]
                        log.info(f"  Price to Beat: ${pm_prices['open_price']:,.2f}")

                    dashboard_state.set_market(current_market)

                    # Start or update WebSocket subscription
                    if config.USE_WEBSOCKET:
                        tokens = [current_market.yes_token_id, current_market.no_token_id]
                        if market_ws is None:
                            market_ws = MarketWebSocket(tokens)
                            market_ws.connect()
                        else:
                            market_ws.update_tokens(tokens)
                else:
                    log.warning("No active market found. Retrying in 30s...")
                    render_dashboard(None, None, None, trader, 0, cycle_count)
                    _sleep(30, running_check=lambda: running)
                    continue

            # ── Step 2: Fetch prices ─────────────────────────────────────
            # Bypass spike filter in last 60s — price convergence is expected
            near_expiry = (current_market.time_remaining is not None
                           and current_market.time_remaining < 60)
            prices = fetch_price_snapshot_hybrid(
                current_market, market_ws, skip_spike_filter=near_expiry,
            )
            last_refresh = time.time()
            dashboard_state.set_prices(prices)

            # Retry price-to-beat scrape if not yet populated — brand-new
            # markets often lack SSR'd oracle data at discovery time. If the
            # scrape is still empty, fall back to Pyth historical at window
            # open (Chainlink-vs-Pyth drift ≈ $5–10, good enough for UI).
            if current_market.strike_price is None:
                pm_prices = fetch_polymarket_prices(current_market.slug)
                if pm_prices.get("open_price"):
                    current_market.strike_price = pm_prices["open_price"]
                    log.info(f"  Price to Beat: ${pm_prices['open_price']:,.2f}")
                elif current_market.event_start_time is not None:
                    ts = int(current_market.event_start_time.timestamp())
                    pyth_open = fetch_pyth_btc_price_at(ts)
                    if pyth_open is not None:
                        current_market.strike_price = pyth_open
                        log.info(f"  Price to Beat: ${pyth_open:,.2f} (Pyth fallback)")
                    else:
                        log.debug("Price-to-beat unresolved: scrape+Pyth both empty")

            dashboard_state.set_market(current_market)  # refresh time_remaining
            dashboard_state.update_trade_pnl(prices)    # live PnL update

            # Fetch BTC price for dashboard: prefer Pyth (matches Polymarket's
            # chart), fall back to Binance/Coinbase if Hermes is unreachable.
            btc_spot = fetch_pyth_btc_price(config.BTC_PRICE_POLL_SECONDS)
            if btc_spot is None:
                btc_spot = fetch_btc_price()
            if btc_spot:
                dashboard_state.set_btc_price(btc_spot)
            if market_ws:
                dashboard_state.set_ws_status(market_ws.is_connected)

            # ── Stop-loss check ──────────────────────────────────────────
            # Skip entirely in the last 15s — book thins near resolution and
            # bid flickers to $0.01 produce spurious PnL crashes. The trade
            # will resolve naturally within seconds.
            near_resolution = (current_market.time_remaining is not None
                               and current_market.time_remaining < 15)
            if config.STOP_LOSS_ENABLED and prices and not near_resolution:
                open_trades = dashboard_state.get_open_trades()
                if open_trades:
                    summaries = ", ".join(
                        f"#{t.get('trade_id')}:${t.get('unrealized_pnl',0):.2f}"
                        for t in open_trades
                    )
                    log.info(
                        f"STOP-LOSS scan: {len(open_trades)} open "
                        f"[{summaries}] threshold=-${config.STOP_LOSS_AMOUNT:.2f} "
                        f"bids=(y={prices.yes_bid}, n={prices.no_bid})"
                    )
                # Drop breach counters for trades no longer open
                open_tids = {t.get("trade_id") for t in open_trades}
                for _tid in list(stop_loss_breach_count.keys()):
                    if _tid not in open_tids:
                        del stop_loss_breach_count[_tid]
                for open_trade in open_trades:
                    pnl = open_trade.get("unrealized_pnl", 0)
                    tid_dbg = open_trade.get("trade_id")
                    if pnl > -config.STOP_LOSS_AMOUNT:
                        # Recovered — clear any prior breach count
                        stop_loss_breach_count.pop(tid_dbg, None)
                    if pnl <= -config.STOP_LOSS_AMOUNT:
                        # Require 2 consecutive breaches to defeat single-tick
                        # bid glitches (crossed/thin book).
                        count = stop_loss_breach_count.get(tid_dbg, 0) + 1
                        stop_loss_breach_count[tid_dbg] = count
                        if count < 2:
                            log.info(
                                f"STOP-LOSS breach #{count} trade #{tid_dbg}: "
                                f"pnl=${pnl:.2f} — waiting for confirmation"
                            )
                            continue
                        log.warning(
                            f"🛑 STOP-LOSS FIRING trade #{tid_dbg}: "
                            f"pnl=${pnl:.2f} <= -${config.STOP_LOSS_AMOUNT:.2f} "
                            f"(confirmed {count} consecutive ticks)"
                        )
                        tid = open_trade["trade_id"]
                        trade_type = open_trade.get("trade_type", "arb")
                        side = open_trade.get("side", "")
                        size = open_trade.get("size", 0)
                        entry_price = open_trade.get("entry_price", 0)

                        if trade_type == "buy_yes" and side == "yes":
                            exit_price = prices.yes_bid
                            if not exit_price:
                                log.warning(f"🛑 STOP-LOSS trade #{tid} SKIPPED — no yes_bid, can't sell")
                                continue  # no bid — can't sell
                            resp = trader.place_limit_order(
                                market=current_market,
                                token_id=current_market.yes_token_id,
                                price=exit_price,
                                size=size,
                                side="SELL",
                            )
                        elif trade_type == "buy_no" and side == "no":
                            exit_price = prices.no_bid
                            if not exit_price:
                                log.warning(f"🛑 STOP-LOSS trade #{tid} SKIPPED — no no_bid, can't sell")
                                continue  # no bid — can't sell
                            resp = trader.place_limit_order(
                                market=current_market,
                                token_id=current_market.no_token_id,
                                price=exit_price,
                                size=size,
                                side="SELL",
                            )
                        elif trade_type == "arb":
                            # Sell both sides of the arb
                            if not prices.yes_bid or not prices.no_bid:
                                log.warning(
                                    f"🛑 STOP-LOSS arb #{tid} SKIPPED — "
                                    f"missing bid (yes={prices.yes_bid}, no={prices.no_bid})"
                                )
                                continue  # need both bids to exit arb
                            exit_price = prices.yes_bid + prices.no_bid
                            resp_y = trader.place_limit_order(
                                market=current_market,
                                token_id=current_market.yes_token_id,
                                price=prices.yes_bid,
                                size=size,
                                side="SELL",
                            )
                            resp_n = trader.place_limit_order(
                                market=current_market,
                                token_id=current_market.no_token_id,
                                price=prices.no_bid,
                                size=size,
                                side="SELL",
                            )
                            resp = resp_y or resp_n
                        else:
                            continue

                        # Compute realized loss from actual exit vs entry prices
                        if trade_type == "arb":
                            cost = open_trade.get("cost")
                            if cost is None:
                                yes_entry = open_trade.get("yes_price")
                                no_entry = open_trade.get("no_price")
                                if yes_entry is not None and no_entry is not None:
                                    cost = (yes_entry + no_entry) * size
                                else:
                                    cost = entry_price * size
                            realized_loss = exit_price * size - cost
                        else:
                            realized_loss = (exit_price - entry_price) * size
                        dashboard_state.stop_loss_trade(tid, realized_loss)
                        log.warning(
                            f"💀 CLOSE stop-loss trade #{tid} "
                            f"({trade_type}/{side}) — "
                            f"entry=${entry_price:.3f} exit=${exit_price:.3f} "
                            f"size={size:.0f} realized=${realized_loss:.2f}"
                        )
                        notify_stop_loss(
                            market_question=open_trade.get("market_question", ""),
                            side=side or trade_type,
                            size=size,
                            entry_price=entry_price,
                            exit_price=exit_price,
                            loss=realized_loss,
                            dry_run=config.DRY_RUN,
                        )

            # ── Market rest period check ─────────────────────────────────
            # Use actual market age (based on end_date for 15-min markets)
            # rather than when the bot first saw the market.
            cid = current_market.condition_id
            if current_market.end_date is not None:
                # 15-min markets: age = 900s - time_remaining
                market_duration = 900  # 15 minutes
                time_left = current_market.time_remaining or 0
                market_age = market_duration - time_left
            else:
                # Fallback: use first-seen time
                market_age = time.time() - market_first_seen.get(cid, 0)
            resting = config.MARKET_REST_SECONDS > 0 and market_age < config.MARKET_REST_SECONDS
            if resting:
                rest_left = config.MARKET_REST_SECONDS - market_age
                log.debug(f"Market resting: {rest_left:.0f}s remaining")
                dashboard_state.set_rest(rest_left)
            else:
                dashboard_state.set_rest(0)

            # ── Step 3: Arbitrage detection ──────────────────────────────
            if prices.yes_ask is None or prices.no_ask is None:
                log.info(
                    f"TRADE LOGIC SKIPPED — missing ask "
                    f"(yes_ask={prices.yes_ask}, no_ask={prices.no_ask})"
                )
            if config.ARB_ENABLED and prices.yes_ask is not None and prices.no_ask is not None:
                # Basic check (1 share)
                arb = detect_arbitrage(current_market, prices, shares=1.0)
                dashboard_state.set_arb(arb)

                # If basic check shows potential, do depth analysis
                if arb.gross_spread > 0:
                    yes_book, no_book = get_books_for_market(current_market)
                    arb = detect_arbitrage_with_depth(
                        current_market, yes_book, no_book, target_size=100
                    )

                    # Find max profitable size
                    if arb.is_profitable:
                        max_size, max_profit = find_max_profitable_size(
                            current_market, yes_book, no_book
                        )
                        arb.max_profitable_size = max_size

                        log_opportunity(arb)
                        log_arb_opportunity(arb)
                        notify_arb_detected(arb)
                        dashboard_state.set_arb(arb)

                        # ── Auto-execute arb if enabled (skip during rest) ──
                        if config.AUTO_EXECUTE and arb.max_profitable_size > 0 and not resting:
                            now = time.time()
                            cooldown_ok = (
                                cid not in last_exec_time
                                or now - last_exec_time[cid] >= config.ARB_COOLDOWN_SECONDS
                            )
                            if (cooldown_ok
                                    and arb.net_profit >= config.ARB_MIN_PROFIT
                                    and arb.roi_pct >= config.ARB_MIN_ROI_PCT):
                                exec_size = min(arb.max_profitable_size, config.MAX_POSITION_SIZE)
                                log.info(
                                    f"⚡ OPEN arb — {exec_size:.0f}×YES@${arb.yes_price:.3f} + "
                                    f"{exec_size:.0f}×NO@${arb.no_price:.3f} "
                                    f"(net=${arb.net_profit:.3f}, roi={arb.roi_pct:.2f}%, "
                                    f"fee={arb.fee_rate_bps}bps)"
                                )
                                yes_resp, no_resp = trader.execute_arbitrage(
                                    market=current_market,
                                    size=exec_size,
                                    yes_price=arb.yes_price,
                                    no_price=arb.no_price,
                                )
                                last_exec_time[cid] = now
                                if yes_resp and no_resp:
                                    last_exec_status = "SUCCESS"
                                elif yes_resp or no_resp:
                                    last_exec_status = "PARTIAL"
                                else:
                                    last_exec_status = "FAILED"
                                log_execution(
                                    market_question=current_market.question,
                                    condition_id=cid,
                                    size=exec_size,
                                    yes_price=arb.yes_price,
                                    no_price=arb.no_price,
                                    net_profit=arb.net_profit,
                                    roi_pct=arb.roi_pct,
                                    status=last_exec_status,
                                )
                                notify_execution(
                                    market_question=current_market.question,
                                    size=exec_size,
                                    yes_price=arb.yes_price,
                                    no_price=arb.no_price,
                                    net_profit=arb.net_profit,
                                    roi_pct=arb.roi_pct,
                                    status=last_exec_status,
                                    dry_run=config.DRY_RUN,
                                )
                                arb_cost = exec_size * (arb.yes_price + arb.no_price)
                                dashboard_state.add_trade(TradeRecord(
                                    timestamp=datetime.now(timezone.utc).isoformat(),
                                    market_question=current_market.question,
                                    condition_id=cid,
                                    size=exec_size,
                                    yes_price=arb.yes_price,
                                    no_price=arb.no_price,
                                    net_profit=arb.net_profit,
                                    roi_pct=arb.roi_pct,
                                    status=last_exec_status,
                                    dry_run=config.DRY_RUN,
                                    trade_type="arb",
                                    cost=arb_cost,
                                    time_remaining=current_market.time_remaining or 0,
                                    market_age=market_age,
                                    combined_ask_at_entry=prices.combined_ask or 0,
                                    fee_rate_bps=current_market.fee_rate_bps,
                                    gross_spread=arb.gross_spread,
                                    yes_bid_at_entry=prices.yes_bid or 0,
                                    no_bid_at_entry=prices.no_bid or 0,
                                    arb_max_size=arb.max_profitable_size,
                                    arb_yes_liquidity=arb.yes_liquidity,
                                    arb_no_liquidity=arb.no_liquidity,
                                ))
                            elif not cooldown_ok:
                                elapsed = now - last_exec_time[cid]
                                log.info(
                                    f"ARB auto-exec BLOCKED — cooldown "
                                    f"({elapsed:.0f}s/{config.ARB_COOLDOWN_SECONDS}s)"
                                )
                            else:
                                log.info(
                                    f"ARB auto-exec BLOCKED — below threshold "
                                    f"(net=${arb.net_profit:.3f}/min ${config.ARB_MIN_PROFIT:.3f}, "
                                    f"roi={arb.roi_pct:.2f}%/min {config.ARB_MIN_ROI_PCT:.2f}%)"
                                )
                        elif config.AUTO_EXECUTE and arb.is_profitable and resting:
                            log.info(
                                f"ARB auto-exec BLOCKED — market resting "
                                f"({config.MARKET_REST_SECONDS - market_age:.0f}s left)"
                            )

            # ── Directional buy triggers (independent of ARB_ENABLED) ────
            if prices.yes_ask is not None and prices.no_ask is not None:
                yes_price = prices.yes_ask
                no_price = prices.no_ask
                placed = directional_buys_placed.get(cid, set())

                if resting and (config.BUY_YES_TRIGGER > 0 or config.BUY_NO_TRIGGER > 0):
                    log.info(
                        f"TRIGGERS BLOCKED — market resting "
                        f"({config.MARKET_REST_SECONDS - market_age:.0f}s left, "
                        f"yes_ask={yes_price:.3f}, no_ask={no_price:.3f})"
                    )

                _try_directional_buy(
                    "yes", yes_price, config.BUY_YES_TRIGGER,
                    current_market.yes_token_id, current_market, prices,
                    market_age, resting, placed, directional_buys_placed, cid, trader,
                )
                _try_directional_buy(
                    "no", no_price, config.BUY_NO_TRIGGER,
                    current_market.no_token_id, current_market, prices,
                    market_age, resting, placed, directional_buys_placed, cid, trader,
                )

            # ── Step 3b: Pre-fetch next market if expiring soon ─────────
            if (current_market and current_market.time_remaining is not None
                    and current_market.time_remaining < 60
                    and (prefetch_thread is None or not prefetch_thread.is_alive())):
                prefetch_thread = threading.Thread(target=_prefetch_next_market, daemon=True)
                prefetch_thread.start()

            # ── Step 4: Render dashboard ─────────────────────────────────
            render_dashboard(
                current_market,
                prices,
                arb,
                trader,
                time.time() - last_refresh,
                cycle_count,
                ws=market_ws,
                last_exec_status=last_exec_status,
            )

            # ── Step 5: Wait for next cycle ──────────────────────────────
            _sleep(config.POLLING_INTERVAL, running_check=lambda: running)

        except KeyboardInterrupt:
            running = False
        except Exception as e:
            log.error(f"Error in main loop: {e}")
            import traceback
            traceback.print_exc()
            _sleep(10, running_check=lambda: running)

    # Clean up with a hard timeout so we never hang
    if market_ws:
        market_ws.disconnect()
    dashboard_state.set_running(False)

    # Run shutdown notification in a thread with timeout
    def _notify():
        try:
            notify_shutdown()
        except Exception:
            pass
    t = threading.Thread(target=_notify, daemon=True)
    t.start()
    t.join(timeout=3)

    log.info("Bot stopped.")


def _sleep(seconds: float, running_check=None):
    """Sleep interruptibly."""
    end = time.time() + seconds
    while time.time() < end:
        if running_check and not running_check():
            return
        time.sleep(min(0.25, seconds))


# ── One-Shot Commands ────────────────────────────────────────────────────────

def cmd_scan():
    """Scan for active 15-min BTC markets and print results."""
    print(f"\n{BOLD}Scanning for 15-min BTC markets...{RESET}\n")
    markets = search_btc_15min_markets(active_only=True)

    if not markets:
        print(f"{YELLOW}No active 15-min BTC markets found.{RESET}")
        print(f"{DIM}This could mean:")
        print(f"  - Markets haven't been created yet for the current window")
        print(f"  - The Gamma API is slow to index new markets")
        print(f"  - Try again in a few minutes{RESET}")
        return

    for i, m in enumerate(markets, 1):
        remaining = format_countdown(m.time_remaining) if m.time_remaining else "unknown"
        print(f"{CYAN}[{i}]{RESET} {BOLD}{m.question}{RESET}")
        print(f"    Slug:           {m.slug}")
        print(f"    Condition ID:   {m.condition_id[:50]}...")
        print(f"    YES token:      {m.yes_token_id[:50]}...")
        print(f"    NO  token:      {m.no_token_id[:50]}...")
        print(f"    Ends in:        {remaining}")
        print(f"    Fee rate:       {m.fee_rate_bps} bps")
        print()


def cmd_arb_check():
    """One-shot arbitrage check on the current market."""
    print(f"\n{BOLD}Checking for arbitrage on current BTC 15-min market...{RESET}\n")

    market = get_current_market()
    if not market:
        print(f"{YELLOW}No active market found.{RESET}")
        return

    print(f"Market: {BOLD}{market.question}{RESET}")
    print(f"Fetching order books...\n")

    prices = fetch_price_snapshot(market)
    yes_book, no_book = get_books_for_market(market)

    print(f"  YES ask: {format_price(prices.yes_ask)}    (bid: {format_price(prices.yes_bid)})")
    print(f"  NO  ask: {format_price(prices.no_ask)}    (bid: {format_price(prices.no_bid)})")

    if prices.combined_ask is not None:
        print(f"  Combined ask: {BOLD}{format_usd(prices.combined_ask)}{RESET}")
        print(f"  Gross spread: {format_usd(1.0 - prices.combined_ask)}")
    print()

    if prices.yes_ask is not None and prices.no_ask is not None:
        arb = detect_arbitrage_with_depth(market, yes_book, no_book, target_size=100)

        if arb.is_profitable:
            print(f"{GREEN}{BOLD}🟢 ARBITRAGE OPPORTUNITY!{RESET}")
            print(f"  Net profit (100 shares): {GREEN}{format_usd(arb.net_profit)}{RESET}")
            print(f"  ROI: {GREEN}{format_pct(arb.roi_pct)}{RESET}")
            print(f"  Total fees: {format_usd(arb.total_fees)}")

            max_size, max_profit = find_max_profitable_size(market, yes_book, no_book)
            if max_size > 0:
                print(f"  Max profitable size: {max_size:.0f} shares (profit: {format_usd(max_profit)})")
        else:
            print(f"{DIM}No arbitrage opportunity at current prices.{RESET}")
            print(f"  Need spread > fees ({format_usd(arb.total_fees)}) to be profitable")
    else:
        print(f"{RED}Could not fetch prices for both sides.{RESET}")
    print()


# ── CLI Entry ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket BTC 15-min Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py               Run the bot (live polling loop)
  python main.py --scan        Scan for active BTC 15-min markets
  python main.py --arb-check   One-shot arbitrage check
        """,
    )
    parser.add_argument("--scan", action="store_true", help="Scan for active markets and exit")
    parser.add_argument("--arb-check", action="store_true", help="Check arbitrage on current market")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Disable web dashboard (terminal only)")
    parser.add_argument("--dashboard-only", action="store_true",
                        help="Start only the web dashboard (no bot)")
    args = parser.parse_args()

    if args.scan:
        cmd_scan()
    elif args.arb_check:
        cmd_arb_check()
    elif args.dashboard_only:
        from dashboard_server import start_dashboard
        log.info("Starting dashboard-only mode (no bot)...")
        start_dashboard(blocking=True)
    else:
        run_bot(enable_dashboard=not args.no_dashboard)


if __name__ == "__main__":
    main()
