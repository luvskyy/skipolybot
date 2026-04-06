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
from market_data import fetch_price_snapshot, fetch_price_snapshot_hybrid, get_books_for_market, MarketWebSocket, spike_filter
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
        for e in errors:
            log.warning(f"Config warning (DRY RUN): {e}")

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
                        if resolution_prices.yes_bid > 0.85:
                            winning_side = "yes"
                        elif resolution_prices.no_bid > 0.85:
                            winning_side = "no"
                    # Update state with true resolution prices before resolving
                    dashboard_state.set_prices(resolution_prices)
                    dashboard_state.update_trade_pnl(resolution_prices)
                    dashboard_state.resolve_trades(current_market.condition_id, winning_side=winning_side)
                    dashboard_state.increment_market_cycle()
                    side_str = winning_side or "unknown"
                    log.info(f"Market expired (winner: {side_str}) — resolved open trades for [{current_market.question[:50]}]")
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
            dashboard_state.set_market(current_market)  # refresh time_remaining
            dashboard_state.update_trade_pnl(prices)    # live PnL update
            if market_ws:
                dashboard_state.set_ws_status(market_ws.is_connected)

            # ── Stop-loss check ──────────────────────────────────────────
            if config.STOP_LOSS_ENABLED and prices:
                for open_trade in dashboard_state.get_open_trades():
                    pnl = open_trade.get("unrealized_pnl", 0)
                    if pnl <= -config.STOP_LOSS_AMOUNT:
                        tid = open_trade["trade_id"]
                        trade_type = open_trade.get("trade_type", "arb")
                        side = open_trade.get("side", "")
                        size = open_trade.get("size", 0)
                        entry_price = open_trade.get("entry_price", 0)

                        if trade_type == "buy_yes" and side == "yes":
                            exit_price = prices.yes_bid
                            if not exit_price:
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
                            cost = open_trade.get("cost", entry_price * size)
                            realized_loss = exit_price * size - cost
                        else:
                            realized_loss = (exit_price - entry_price) * size
                        dashboard_state.stop_loss_trade(tid, realized_loss)
                        log.warning(
                            f"🛑 Stop-loss triggered on trade #{tid} "
                            f"({trade_type}): lost ${abs(realized_loss):.2f}"
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
                                log.info(f"Auto-executing arb: {exec_size:.0f} shares")
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
                                log.debug(f"Arb cooldown active for {cid[:20]}...")

                # ── Directional buy triggers (skip during rest) ─────────
                if not resting:
                    placed = directional_buys_placed.get(cid, set())
                    yes_price = prices.yes_ask
                    no_price = prices.no_ask

                    # YES trigger: buy YES when price >= threshold
                    if (config.BUY_YES_TRIGGER > 0
                            and yes_price >= config.BUY_YES_TRIGGER
                            and "yes" not in placed):
                        buy_size = min(config.DIRECTIONAL_BUY_SIZE, config.MAX_POSITION_SIZE)
                        log.info(f"📈 YES trigger hit ({yes_price:.2f} >= {config.BUY_YES_TRIGGER:.2f}), buying {buy_size:.0f} shares")
                        resp = trader.place_limit_order(
                            market=current_market,
                            token_id=current_market.yes_token_id,
                            price=yes_price,
                            size=buy_size,
                            side="BUY",
                        )
                        placed.add("yes")
                        directional_buys_placed[cid] = placed
                        status = "SUCCESS" if resp else "FAILED"
                        yes_cost = buy_size * yes_price
                        dashboard_state.add_trade(TradeRecord(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            market_question=current_market.question,
                            condition_id=cid,
                            size=buy_size,
                            yes_price=yes_price,
                            no_price=0,
                            net_profit=0,
                            roi_pct=0,
                            status=status,
                            dry_run=config.DRY_RUN,
                            trade_type="buy_yes",
                            cost=yes_cost,
                            side="yes",
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
                            yes_price=yes_price,
                            no_price=0,
                            net_profit=0,
                            roi_pct=0,
                            status=f"YES BUY {status}",
                            dry_run=config.DRY_RUN,
                        )

                    # NO trigger: buy NO when price >= threshold
                    if (config.BUY_NO_TRIGGER > 0
                            and no_price >= config.BUY_NO_TRIGGER
                            and "no" not in placed):
                        buy_size = min(config.DIRECTIONAL_BUY_SIZE, config.MAX_POSITION_SIZE)
                        log.info(f"📉 NO trigger hit ({no_price:.2f} >= {config.BUY_NO_TRIGGER:.2f}), buying {buy_size:.0f} shares")
                        resp = trader.place_limit_order(
                            market=current_market,
                            token_id=current_market.no_token_id,
                            price=no_price,
                            size=buy_size,
                            side="BUY",
                        )
                        placed.add("no")
                        directional_buys_placed[cid] = placed
                        status = "SUCCESS" if resp else "FAILED"
                        no_cost = buy_size * no_price
                        dashboard_state.add_trade(TradeRecord(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            market_question=current_market.question,
                            condition_id=cid,
                            size=buy_size,
                            yes_price=0,
                            no_price=no_price,
                            net_profit=0,
                            roi_pct=0,
                            status=status,
                            dry_run=config.DRY_RUN,
                            trade_type="buy_no",
                            cost=no_cost,
                            side="no",
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
                            yes_price=0,
                            no_price=no_price,
                            net_profit=0,
                            roi_pct=0,
                            status=f"NO BUY {status}",
                            dry_run=config.DRY_RUN,
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
