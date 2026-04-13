"""
Shared bot state — thread-safe store that the bot writes to and the dashboard reads.

This module acts as the bridge between the existing bot loop (main.py) and the
web dashboard (dashboard_server.py). All data is kept in memory with a fixed-size
history for charts and logs.
"""

import collections
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from models import ArbitrageOpportunity, Market, PriceSnapshot


_MAX_LOG_LINES = 500
_MAX_PRICE_HISTORY = 360     # ~30 min at 5s polling
_MAX_ARB_HISTORY = 200
_MAX_TRADE_HISTORY = 200
_MAX_TRADE_PRICE_TICKS = 360  # per-trade price history cap


@dataclass
class TradeRecord:
    timestamp: str
    market_question: str
    condition_id: str
    size: float
    yes_price: float
    no_price: float
    net_profit: float
    roi_pct: float
    status: str
    dry_run: bool = True
    trade_type: str = "arb"  # "arb", "buy_yes", "buy_no"
    cost: float = 0.0        # total investment (size * price)
    unrealized_pnl: float = 0.0   # live PnL based on current prices
    resolved: bool = False         # True when market has ended
    side: str = ""                 # "yes" or "no" (for directional trades)
    # Rich context captured at trade time
    time_remaining: float = 0.0       # seconds left on market when trade was placed
    market_age: float = 0.0           # seconds since market opened
    combined_ask_at_entry: float = 0.0  # combined YES+NO ask at entry
    fee_rate_bps: int = 0             # fee rate in basis points
    gross_spread: float = 0.0         # 1.0 - combined_ask at entry
    yes_bid_at_entry: float = 0.0
    no_bid_at_entry: float = 0.0
    arb_max_size: float = 0.0         # max profitable arb size at entry
    arb_yes_liquidity: float = 0.0    # YES book liquidity at entry
    arb_no_liquidity: float = 0.0     # NO book liquidity at entry


class BotState:
    """Thread-safe singleton holding all dashboard-relevant state."""

    def __init__(self):
        self._lock = threading.Lock()

        # Connection / uptime
        self.start_time: float = time.time()
        self.bot_running: bool = False
        self.dry_run: bool = True
        self.auto_execute: bool = False
        self.use_websocket: bool = True
        self.ws_connected: bool = False
        self.polling_interval: float = 5
        self.cycle_count: int = 0
        self.market_cycles: int = 1     # starts at 1 (bot begins on its first market)
        self.rest_remaining: float = 0  # seconds left in market rest period

        # Current market
        self.current_market: Optional[dict] = None

        # Current prices
        self.current_prices: Optional[dict] = None

        # Current arbitrage
        self.current_arb: Optional[dict] = None

        # BTC spot price
        self.btc_price: Optional[float] = None
        self.btc_price_history: collections.deque = collections.deque(maxlen=_MAX_PRICE_HISTORY)

        # Historical data for charts
        self.price_history: collections.deque = collections.deque(maxlen=_MAX_PRICE_HISTORY)
        self.arb_history: collections.deque = collections.deque(maxlen=_MAX_ARB_HISTORY)

        # Trade log
        self.trades: collections.deque = collections.deque(maxlen=_MAX_TRADE_HISTORY)
        self._next_trade_id: int = 1
        # Per-trade price histories: trade_id -> deque of price snapshots
        self._trade_price_histories: dict[int, collections.deque] = {}
        # Per-trade price history at time of entry (snapshot of global history)
        self._trade_entry_history: dict[int, list] = {}
        self.total_pnl: float = 0.0
        self.session_pnl: float = 0.0   # cumulative PnL across ALL resolved markets
        self._session_resolved_cids: set = set()  # condition_ids already counted in session_pnl
        self.total_trades: int = 0
        self.winning_trades: int = 0

        # Bot log lines (captured from logger)
        self.log_lines: collections.deque = collections.deque(maxlen=_MAX_LOG_LINES)

        # Revision counter — bumped on every change so SSE knows when to push
        self._revision: int = 0

    @property
    def revision(self) -> int:
        return self._revision

    def _bump(self):
        self._revision += 1

    # ── Writers (called from bot loop) ───────────────────────────────────

    def set_running(self, running: bool):
        with self._lock:
            self.bot_running = running
            self._bump()

    def set_config(self, dry_run: bool, auto_execute: bool, use_websocket: bool,
                   polling_interval: int):
        with self._lock:
            self.dry_run = dry_run
            self.auto_execute = auto_execute
            self.use_websocket = use_websocket
            self.polling_interval = polling_interval
            self._bump()

    def get_settings(self) -> dict:
        """Return current settings from config module."""
        import config
        return {
            "MAX_POSITION_SIZE": config.MAX_POSITION_SIZE,
            "MAX_BUDGET": config.MAX_BUDGET,
            "MAX_CONCURRENT_POSITIONS": config.MAX_CONCURRENT_POSITIONS,
            "ARB_MIN_PROFIT": config.ARB_MIN_PROFIT,
            "ARB_MIN_ROI_PCT": config.ARB_MIN_ROI_PCT,
            "MAX_LOSS_PER_TRADE": config.MAX_LOSS_PER_TRADE,
            "MAX_DAILY_LOSS": config.MAX_DAILY_LOSS,
            "STOP_LOSS_ENABLED": config.STOP_LOSS_ENABLED,
            "STOP_LOSS_AMOUNT": config.STOP_LOSS_AMOUNT,
            "DRY_RUN": config.DRY_RUN,
            "AUTO_EXECUTE": config.AUTO_EXECUTE,
            "ARB_COOLDOWN_SECONDS": config.ARB_COOLDOWN_SECONDS,
            "POLLING_INTERVAL": config.POLLING_INTERVAL,
            "USE_WEBSOCKET": config.USE_WEBSOCKET,
            "BUY_YES_TRIGGER": config.BUY_YES_TRIGGER,
            "BUY_NO_TRIGGER": config.BUY_NO_TRIGGER,
            "DIRECTIONAL_BUY_SIZE": config.DIRECTIONAL_BUY_SIZE,
            "MARKET_REST_SECONDS": config.MARKET_REST_SECONDS,
            "SPIKE_THRESHOLD": config.SPIKE_THRESHOLD,
            "ARB_ENABLED": config.ARB_ENABLED,
            "BTC_PRICE_POLL_SECONDS": config.BTC_PRICE_POLL_SECONDS,
        }

    def set_settings(self, settings: dict) -> dict:
        """Update config module values at runtime and sync bot_state fields."""
        import config

        # Validation rules: key -> (type, min, max)
        validators = {
            "MAX_POSITION_SIZE": (float, 1, 10000),
            "MAX_BUDGET": (float, 0, 1000000),
            "MAX_CONCURRENT_POSITIONS": (int, 1, 50),
            "ARB_MIN_PROFIT": (float, 0, 100),
            "ARB_MIN_ROI_PCT": (float, 0, 100),
            "MAX_LOSS_PER_TRADE": (float, 0, 10000),
            "MAX_DAILY_LOSS": (float, 0, 100000),
            "STOP_LOSS_ENABLED": (bool, None, None),
            "STOP_LOSS_AMOUNT": (float, 0, 1000000),
            "DRY_RUN": (bool, None, None),
            "AUTO_EXECUTE": (bool, None, None),
            "ARB_COOLDOWN_SECONDS": (int, 0, 3600),
            "POLLING_INTERVAL": (float, 0.25, 300),
            "USE_WEBSOCKET": (bool, None, None),
            "BUY_YES_TRIGGER": (float, 0, 1),
            "BUY_NO_TRIGGER": (float, 0, 1),
            "MAX_BUY_PRICE": (float, 0, 1),
            "DIRECTIONAL_BUY_SIZE": (float, 1, 10000),
            "MARKET_REST_SECONDS": (int, 0, 900),
            "SPIKE_THRESHOLD": (float, 0.01, 0.5),
            "ARB_ENABLED": (bool, None, None),
            "BTC_PRICE_POLL_SECONDS": (float, 0.5, 60),
        }

        errors = []
        for key, value in settings.items():
            if key not in validators:
                errors.append(f"Unknown setting: {key}")
                continue
            expected_type, vmin, vmax = validators[key]
            if expected_type == bool:
                if not isinstance(value, bool):
                    errors.append(f"{key} must be a boolean")
                    continue
            else:
                try:
                    value = expected_type(value)
                except (TypeError, ValueError):
                    errors.append(f"{key} must be {expected_type.__name__}")
                    continue
                if vmin is not None and value < vmin:
                    errors.append(f"{key} must be >= {vmin}")
                    continue
                if vmax is not None and value > vmax:
                    errors.append(f"{key} must be <= {vmax}")
                    continue
            setattr(config, key, value)

        if errors:
            return {"ok": False, "errors": errors}

        # Sync bot_state fields that mirror config
        with self._lock:
            self.dry_run = config.DRY_RUN
            self.auto_execute = config.AUTO_EXECUTE
            self.use_websocket = config.USE_WEBSOCKET
            self.polling_interval = config.POLLING_INTERVAL
            self._bump()

        return {"ok": True, "settings": self.get_settings()}

    def increment_market_cycle(self):
        with self._lock:
            self.market_cycles += 1
            self._bump()

    def set_rest(self, remaining: float):
        with self._lock:
            self.rest_remaining = max(0, remaining)
            self._bump()

    def set_cycle(self, count: int):
        with self._lock:
            self.cycle_count = count
            self._bump()

    def set_ws_status(self, connected: bool):
        with self._lock:
            self.ws_connected = connected
            self._bump()

    def set_market(self, market: Optional[Market]):
        with self._lock:
            if market is None:
                self.current_market = None
            else:
                self.current_market = {
                    "question": market.question,
                    "condition_id": market.condition_id,
                    "slug": market.slug,
                    "yes_token_id": market.yes_token_id[:30] + "...",
                    "no_token_id": market.no_token_id[:30] + "...",
                    "end_date": market.end_date.isoformat() if market.end_date else None,
                    "active": market.active,
                    "fee_rate_bps": market.fee_rate_bps,
                    "time_remaining": market.time_remaining,
                    "strike_price": market.strike_price,
                }
            self._bump()

    def set_btc_price(self, btc_price: Optional[float]):
        with self._lock:
            if btc_price is not None:
                self.btc_price = btc_price
                self.btc_price_history.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "price": btc_price,
                })
            self._bump()

    def set_prices(self, prices: Optional[PriceSnapshot]):
        with self._lock:
            if prices is None:
                self.current_prices = None
            else:
                snap = {
                    "timestamp": prices.timestamp.isoformat(),
                    "yes_bid": prices.yes_bid,
                    "yes_ask": prices.yes_ask,
                    "no_bid": prices.no_bid,
                    "no_ask": prices.no_ask,
                    "combined_ask": prices.combined_ask,
                    "combined_bid": prices.combined_bid,
                }
                self.current_prices = snap
                self.price_history.append(snap)

                # Append tick to all open (unresolved) trade histories
                for trade in self.trades:
                    if trade.get("resolved"):
                        continue
                    tid = trade.get("trade_id")
                    if tid and tid in self._trade_price_histories:
                        self._trade_price_histories[tid].append(snap)
            self._bump()

    def set_arb(self, arb: Optional[ArbitrageOpportunity]):
        with self._lock:
            if arb is None:
                self.current_arb = None
            else:
                data = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market_question": arb.market_question,
                    "yes_price": arb.yes_price,
                    "no_price": arb.no_price,
                    "combined_cost": arb.combined_cost,
                    "fee_rate_bps": arb.fee_rate_bps,
                    "fee_yes": arb.fee_yes,
                    "fee_no": arb.fee_no,
                    "total_fees": arb.total_fees,
                    "gross_spread": arb.gross_spread,
                    "net_profit": arb.net_profit,
                    "roi_pct": arb.roi_pct,
                    "is_profitable": arb.is_profitable,
                    "yes_liquidity": arb.yes_liquidity,
                    "no_liquidity": arb.no_liquidity,
                    "max_profitable_size": arb.max_profitable_size,
                }
                self.current_arb = data
                self.arb_history.append(data)
            self._bump()

    def add_trade(self, trade: TradeRecord):
        with self._lock:
            trade_id = self._next_trade_id
            self._next_trade_id += 1

            self.trades.appendleft({
                "trade_id": trade_id,
                "timestamp": trade.timestamp,
                "market_question": trade.market_question,
                "condition_id": trade.condition_id,
                "size": trade.size,
                "yes_price": trade.yes_price,
                "no_price": trade.no_price,
                "net_profit": trade.net_profit,
                "roi_pct": trade.roi_pct,
                "trade_type": trade.trade_type,
                "cost": trade.cost,
                "status": trade.status,
                "dry_run": trade.dry_run,
                "unrealized_pnl": trade.unrealized_pnl,
                "resolved": trade.resolved,
                "side": trade.side,
                "entry_price": trade.yes_price if trade.side == "yes" else trade.no_price,
                # Rich context
                "time_remaining": trade.time_remaining,
                "market_age": trade.market_age,
                "combined_ask_at_entry": trade.combined_ask_at_entry,
                "fee_rate_bps": trade.fee_rate_bps,
                "gross_spread": trade.gross_spread,
                "yes_bid_at_entry": trade.yes_bid_at_entry,
                "no_bid_at_entry": trade.no_bid_at_entry,
                "arb_max_size": trade.arb_max_size,
                "arb_yes_liquidity": trade.arb_yes_liquidity,
                "arb_no_liquidity": trade.arb_no_liquidity,
                # Resolution fields (populated later)
                "end_yes_price": None,
                "end_no_price": None,
                "resolution_time": None,
            })

            # Snapshot the global price history at time of trade
            self._trade_entry_history[trade_id] = list(self.price_history)
            # Start tracking per-trade price ticks going forward
            self._trade_price_histories[trade_id] = collections.deque(
                maxlen=_MAX_TRADE_PRICE_TICKS
            )

            self.total_trades += 1
            if trade.trade_type == "arb":
                self.total_pnl += trade.net_profit
                if trade.net_profit > 0:
                    self.winning_trades += 1
            self._bump()

    def update_trade_pnl(self, prices: Optional[PriceSnapshot]):
        """Update unrealized PnL for all open (unresolved) trades based on current prices."""
        if prices is None:
            return
        yes_bid = prices.yes_bid
        no_bid = prices.no_bid

        with self._lock:
            unrealized_total = 0.0
            for trade in self.trades:
                if trade.get("resolved"):
                    continue
                trade_type = trade.get("trade_type", "arb")
                size = trade.get("size", 0)

                if trade_type == "arb":
                    # Arb: profit is locked in at entry, but show live mark-to-market
                    # Value of both positions = (yes_bid + no_bid) * size
                    # PnL = value - cost
                    # Skip update if either bid is None (no market data)
                    if yes_bid is None or no_bid is None:
                        unrealized_total += trade.get("unrealized_pnl", 0)
                        continue
                    cost = trade.get("cost", 0)
                    value = (yes_bid + no_bid) * size
                    trade["unrealized_pnl"] = value - cost
                elif trade_type == "buy_yes":
                    # Skip update if bid is None — don't treat missing data as $0
                    if yes_bid is None:
                        unrealized_total += trade.get("unrealized_pnl", 0)
                        continue
                    entry = trade.get("entry_price", trade.get("yes_price", 0))
                    current = yes_bid
                    trade["unrealized_pnl"] = (current - entry) * size
                elif trade_type == "buy_no":
                    # Skip update if bid is None — don't treat missing data as $0
                    if no_bid is None:
                        unrealized_total += trade.get("unrealized_pnl", 0)
                        continue
                    entry = trade.get("entry_price", trade.get("no_price", 0))
                    current = no_bid
                    trade["unrealized_pnl"] = (current - entry) * size

                unrealized_total += trade.get("unrealized_pnl", 0)

            # Total PnL = realized (arb) + unrealized (open positions)
            realized = sum(
                t.get("net_profit", 0) for t in self.trades
                if t.get("resolved") and t.get("trade_type") == "arb"
            )
            self.total_pnl = realized + unrealized_total
            self._bump()

    def resolve_trades(self, condition_id: str, winning_side: Optional[str] = None):
        """
        Mark all trades for a market as resolved when it ends.

        For directional trades, if winning_side is unknown we resolve at
        last known unrealized PnL. If winning_side is provided ("yes"/"no"),
        winning positions pay out $1/share, losing positions pay $0.
        """
        with self._lock:
            for trade in self.trades:
                if trade.get("condition_id") != condition_id:
                    continue
                if trade.get("resolved"):
                    continue

                trade["resolved"] = True
                trade["resolution_time"] = datetime.now(timezone.utc).isoformat()
                # Capture end prices from last known state
                if self.current_prices:
                    trade["end_yes_price"] = self.current_prices.get("yes_bid")
                    trade["end_no_price"] = self.current_prices.get("no_bid")

                trade_type = trade.get("trade_type", "arb")
                size = trade.get("size", 0)

                if trade_type == "arb":
                    # Arb always resolves at $1/share payout
                    cost = trade.get("cost", 0)
                    pnl = size - cost  # $1 * size - cost
                    trade["net_profit"] = pnl
                    trade["unrealized_pnl"] = 0
                    trade["status"] = "RESOLVED"
                elif winning_side and trade.get("side"):
                    entry = trade.get("entry_price", 0)
                    if trade.get("side") == winning_side:
                        pnl = (1.0 - entry) * size  # won: payout $1/share
                    else:
                        pnl = -entry * size  # lost: shares worth $0
                    trade["net_profit"] = pnl
                    trade["unrealized_pnl"] = 0
                    trade["status"] = "WON" if pnl > 0 else "LOST"
                else:
                    # No winning side known — lock in last unrealized PnL
                    trade["net_profit"] = trade.get("unrealized_pnl", 0)
                    trade["unrealized_pnl"] = 0
                    trade["status"] = "RESOLVED"

            # Recalculate total PnL
            self.total_pnl = sum(t.get("net_profit", 0) for t in self.trades if t.get("resolved"))
            unrealized = sum(t.get("unrealized_pnl", 0) for t in self.trades if not t.get("resolved"))
            self.total_pnl += unrealized
            self.winning_trades = sum(
                1 for t in self.trades if t.get("resolved") and t.get("net_profit", 0) > 0
            )

            # Accumulate session PnL (only once per condition_id to avoid double-counting)
            if condition_id not in self._session_resolved_cids:
                self._session_resolved_cids.add(condition_id)
                resolved_pnl = sum(t.get("net_profit", 0) for t in self.trades
                                   if t.get("resolved") and t.get("condition_id") == condition_id)
                self.session_pnl += resolved_pnl
            self._bump()

    def stop_loss_trade(self, trade_id: int, realized_pnl: float):
        """Mark a trade as stopped out and realize the loss."""
        with self._lock:
            for trade in self.trades:
                if trade.get("trade_id") != trade_id:
                    continue
                if trade.get("resolved"):
                    return
                trade["resolved"] = True
                trade["status"] = "STOPPED"
                trade["net_profit"] = realized_pnl
                trade["unrealized_pnl"] = 0
                trade["resolution_time"] = datetime.now(timezone.utc).isoformat()
                if self.current_prices:
                    trade["end_yes_price"] = self.current_prices.get("yes_bid")
                    trade["end_no_price"] = self.current_prices.get("no_bid")

                # Recalculate totals
                self.total_pnl = sum(t.get("net_profit", 0) for t in self.trades if t.get("resolved"))
                unrealized = sum(t.get("unrealized_pnl", 0) for t in self.trades if not t.get("resolved"))
                self.total_pnl += unrealized
                self.winning_trades = sum(
                    1 for t in self.trades if t.get("resolved") and t.get("net_profit", 0) > 0
                )

                # Update session PnL (once per condition_id, same pattern as resolve_trades)
                cid = trade.get("condition_id", "")
                if cid and cid not in self._session_resolved_cids:
                    self._session_resolved_cids.add(cid)
                    resolved_pnl = sum(t.get("net_profit", 0) for t in self.trades
                                       if t.get("resolved") and t.get("condition_id") == cid)
                    self.session_pnl += resolved_pnl
                elif cid:
                    # Already counted this cid — add just this trade's realized PnL
                    self.session_pnl += realized_pnl

                self._bump()
                return

    def get_open_trades(self) -> list[dict]:
        """Return all unresolved trades with their current state."""
        with self._lock:
            return [dict(t) for t in self.trades if not t.get("resolved")]

    def get_trade_detail(self, trade_id: int) -> Optional[dict]:
        """Return full detail for a single trade including its price history."""
        with self._lock:
            trade = None
            for t in self.trades:
                if t.get("trade_id") == trade_id:
                    trade = dict(t)
                    break
            if trade is None:
                return None

            # Combine entry history + post-trade ticks for full timeline
            entry_history = self._trade_entry_history.get(trade_id, [])
            post_trade_ticks = list(self._trade_price_histories.get(trade_id, []))

            trade["price_history_before"] = entry_history
            trade["price_history_after"] = post_trade_ticks

            return trade

    def add_log(self, line: str):
        with self._lock:
            self.log_lines.append(line)
            self._bump()

    # ── Readers (called from dashboard) ──────────────────────────────────

    def snapshot(self) -> dict:
        """Return a full JSON-serializable snapshot of all state."""
        with self._lock:
            return {
                "revision": self._revision,
                "uptime_seconds": time.time() - self.start_time,
                "bot_running": self.bot_running,
                "dry_run": self.dry_run,
                "auto_execute": self.auto_execute,
                "use_websocket": self.use_websocket,
                "ws_connected": self.ws_connected,
                "polling_interval": self.polling_interval,
                "cycle_count": self.cycle_count,
                "market_cycles": self.market_cycles,
                "rest_remaining": self.rest_remaining,
                "market": self.current_market,
                "prices": self.current_prices,
                "btc_price": self.btc_price,
                "btc_price_history": list(self.btc_price_history),
                "arb": self.current_arb,
                "price_history": list(self.price_history),
                "arb_history": list(self.arb_history),
                "trades": list(self.trades),
                "total_pnl": self.total_pnl,
                "session_pnl": self.session_pnl,
                "total_trades": self.total_trades,
                "winning_trades": self.winning_trades,
                "logs": list(self.log_lines)[-100:],
            }


# ── Singleton ──────────────────────────────────────────────────────────────

state = BotState()


# ── Log Handler Integration ────────────────────────────────────────────────

import logging


class DashboardLogHandler(logging.Handler):
    """Captures log records into the shared bot state for dashboard display."""

    def __init__(self, bot_state: BotState):
        super().__init__()
        self._state = bot_state

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._state.add_log(msg)
        except Exception:
            pass
