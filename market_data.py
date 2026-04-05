"""
Market data — fetch prices, order books, and WebSocket streaming.
"""

import json
import requests
import threading
import time as _time
from datetime import datetime, timezone
from typing import Optional

import websocket

import config
from models import Market, OrderBook, OrderBookLevel, OrderBookSide, PriceSnapshot
from utils import log


CLOB = config.CLOB_HOST


# ── Spike Filter ────────────────────────────────────────────────────────────

class SpikeFilter:
    """
    Reject price ticks that jump too far from the recent value.

    When a spike is detected, the price is cross-validated against REST.
    If REST confirms the move, the new price is accepted; otherwise it's
    discarded and the last known good price is returned.
    """

    def __init__(self, threshold: float = 0.15):
        self._last: dict[str, float] = {}  # token_id -> last accepted price
        self._threshold = threshold

    def check(self, token_id: str, new_price: float | None) -> tuple[float | None, bool]:
        """
        Validate a new price tick.

        Returns:
            (accepted_price, was_spike) — accepted_price is the price to use
            (may be the old price if spike was rejected), was_spike flags if
            a spike was detected.
        """
        if new_price is None:
            return None, False

        last = self._last.get(token_id)
        if last is None:
            # First tick — accept it
            self._last[token_id] = new_price
            return new_price, False

        delta = abs(new_price - last)
        if delta <= self._threshold:
            # Normal move
            self._last[token_id] = new_price
            return new_price, False

        # Spike detected — cross-validate with REST
        log.warning(
            f"Spike detected: {token_id[:16]}… moved {last:.3f} → {new_price:.3f} "
            f"(Δ{delta:.3f} > {self._threshold}). Confirming via REST…"
        )
        rest_price = fetch_price(token_id, "BUY")
        if rest_price is not None and abs(rest_price - new_price) < self._threshold:
            # REST confirms the move — accept it
            log.info(f"Spike confirmed by REST ({rest_price:.3f}). Accepting.")
            self._last[token_id] = new_price
            return new_price, True
        else:
            # REST disagrees — reject the spike
            rest_str = f"{rest_price:.3f}" if rest_price is not None else "N/A"
            log.warning(
                f"Spike REJECTED (REST says {rest_str}). "
                f"Keeping last good price {last:.3f}."
            )
            return last, True

    def reset(self, token_id: str | None = None):
        """Clear history for a token, or all tokens."""
        if token_id:
            self._last.pop(token_id, None)
        else:
            self._last.clear()


# Global spike filter instance
spike_filter = SpikeFilter(threshold=config.SPIKE_THRESHOLD)


# ── REST Price Fetching ──────────────────────────────────────────────────────

def fetch_price(token_id: str, side: str = "BUY") -> Optional[float]:
    """
    Fetch the current best price for a token from the CLOB API.

    Args:
        token_id: The token to price.
        side: 'BUY' (best ask) or 'SELL' (best bid).

    Returns:
        Price as float, or None on failure.
    """
    try:
        resp = requests.get(
            f"{CLOB}/price",
            params={"token_id": token_id, "side": side},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price")
        if price is not None:
            return float(price)
    except Exception as e:
        log.debug(f"Price fetch failed ({side}) for {token_id[:20]}...: {e}")
    return None


def fetch_order_book(token_id: str) -> OrderBook:
    """
    Fetch the full order book for a token from the CLOB API.

    Returns an OrderBook with sorted bids (desc) and asks (asc).
    """
    book = OrderBook(token_id=token_id)
    try:
        resp = requests.get(
            f"{CLOB}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        # Parse bids — sorted highest first
        raw_bids = data.get("bids", [])
        bid_levels = []
        for b in raw_bids:
            try:
                bid_levels.append(OrderBookLevel(
                    price=float(b.get("price", 0)),
                    size=float(b.get("size", 0)),
                ))
            except (ValueError, TypeError):
                continue
        bid_levels.sort(key=lambda x: x.price, reverse=True)
        book.bids = OrderBookSide(levels=bid_levels)

        # Parse asks — sorted lowest first
        raw_asks = data.get("asks", [])
        ask_levels = []
        for a in raw_asks:
            try:
                ask_levels.append(OrderBookLevel(
                    price=float(a.get("price", 0)),
                    size=float(a.get("size", 0)),
                ))
            except (ValueError, TypeError):
                continue
        ask_levels.sort(key=lambda x: x.price)
        book.asks = OrderBookSide(levels=ask_levels)

    except Exception as e:
        log.debug(f"Order book fetch failed for {token_id[:20]}...: {e}")

    return book


def fetch_price_snapshot(market: Market) -> PriceSnapshot:
    """
    Fetch a complete price snapshot for both sides of a market.
    Uses the order book for accurate best bid/ask.
    """
    yes_book = fetch_order_book(market.yes_token_id)
    no_book = fetch_order_book(market.no_token_id)

    # Run spike filter on REST prices too
    yes_ask, _ = spike_filter.check(market.yes_token_id, yes_book.best_ask)
    no_ask, _ = spike_filter.check(market.no_token_id, no_book.best_ask)

    return PriceSnapshot(
        timestamp=datetime.now(timezone.utc),
        yes_bid=yes_book.best_bid,
        yes_ask=yes_ask,
        no_bid=no_book.best_bid,
        no_ask=no_ask,
    )


def fetch_midpoints(market: Market) -> tuple[Optional[float], Optional[float]]:
    """Quick fetch of YES and NO midpoint prices."""
    yes_buy = fetch_price(market.yes_token_id, "BUY")
    no_buy = fetch_price(market.no_token_id, "BUY")
    return yes_buy, no_buy


# ── Order Book Analysis ──────────────────────────────────────────────────────

def compute_fill_price(book_side: OrderBookSide, size: float) -> Optional[float]:
    """
    Walk the order book to compute the volume-weighted average fill price
    for a given size. Used for slippage estimation.

    Args:
        book_side: The asks side (for buying) or bids side (for selling).
        size: Number of shares to fill.

    Returns:
        Average fill price, or None if insufficient liquidity.
    """
    if not book_side.levels:
        return None

    remaining = size
    total_cost = 0.0

    for level in book_side.levels:
        take = min(remaining, level.size)
        total_cost += take * level.price
        remaining -= take
        if remaining <= 0:
            break

    if remaining > 0:
        # Not enough liquidity
        return None

    return total_cost / size


def get_books_for_market(market: Market) -> tuple[OrderBook, OrderBook]:
    """Fetch order books for both YES and NO tokens."""
    yes_book = fetch_order_book(market.yes_token_id)
    no_book = fetch_order_book(market.no_token_id)
    return yes_book, no_book


# ── Hybrid Price Fetching ───────────────────────────────────────────────────

def fetch_price_snapshot_hybrid(
    market: Market,
    ws: "MarketWebSocket | None" = None,
    max_ws_age: float = 10.0,
) -> PriceSnapshot:
    """
    Fetch prices using WebSocket data when fresh, falling back to REST.

    WS data only provides ask prices. Bid data will be None when sourced
    from the WebSocket — callers needing full book data should use
    get_books_for_market() separately.
    """
    if ws and ws.is_connected:
        yes_bid, yes_ask, yes_age = ws.get_bid_ask(market.yes_token_id)
        no_bid, no_ask, no_age = ws.get_bid_ask(market.no_token_id)

        if (yes_ask is not None and no_ask is not None
                and yes_age < max_ws_age and no_age < max_ws_age):
            # Run spike filter on WS prices before accepting
            yes_ask, yes_spiked = spike_filter.check(market.yes_token_id, yes_ask)
            no_ask, no_spiked = spike_filter.check(market.no_token_id, no_ask)
            if yes_spiked or no_spiked:
                log.info("Spike filter engaged — prices adjusted")
            log.debug("Using WebSocket prices")
            return PriceSnapshot(
                timestamp=datetime.now(timezone.utc),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
            )

    return fetch_price_snapshot(market)


# ── WebSocket Streaming ─────────────────────────────────────────────────────

class MarketWebSocket:
    """
    Real-time price stream from the Polymarket CLOB WebSocket.

    Runs in a daemon thread with auto-reconnect and 10s ping keepalive.
    Exposes a thread-safe get_price() method for the main loop to read.
    """

    def __init__(self, token_ids: list[str]):
        self.token_ids = token_ids
        self._ws: websocket.WebSocketApp | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._prices: dict[str, dict] = {}
        self._connected = False
        self._last_message_time: float = 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_fresh(self) -> bool:
        """True if we received a message within the last 15 seconds."""
        return (_time.time() - self._last_message_time) < 15.0

    def get_price(self, token_id: str) -> tuple[float | None, float]:
        """
        Get the latest best_ask price for a token.
        Returns (price, age_in_seconds). price is None if no data.
        """
        with self._lock:
            entry = self._prices.get(token_id)
            if entry is None:
                return None, float("inf")
            age = _time.time() - entry["timestamp"]
            return entry.get("best_ask"), age

    def get_bid_ask(self, token_id: str) -> tuple[float | None, float | None, float]:
        """
        Get the latest best bid and ask for a token.
        Returns (best_bid, best_ask, age_in_seconds).
        """
        with self._lock:
            entry = self._prices.get(token_id)
            if entry is None:
                return None, None, float("inf")
            age = _time.time() - entry["timestamp"]
            return entry.get("best_bid"), entry.get("best_ask"), age

    def connect(self):
        """Start the WebSocket connection in a daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info("WebSocket: connecting in background thread")

    def disconnect(self):
        """Disconnect and stop the background thread."""
        self._running = False
        self._connected = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def update_tokens(self, token_ids: list[str]):
        """Update subscription to new token IDs (e.g., on market rotation)."""
        self.token_ids = token_ids
        with self._lock:
            self._prices.clear()
        if self._connected and self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            # _run_loop will auto-reconnect with new token_ids

    def _run_loop(self):
        """Reconnect loop — runs in daemon thread."""
        while self._running:
            try:
                self._connect_once()
            except Exception as e:
                log.debug(f"WebSocket error: {e}")
            if self._running:
                self._connected = False
                log.info("WebSocket: reconnecting in 5s...")
                _time.sleep(5)

    def _connect_once(self):
        """Single WebSocket connection attempt with ping keepalive."""

        def on_open(ws):
            self._connected = True
            sub_msg = json.dumps({
                "type": "market",
                "assets_ids": self.token_ids,
                "custom_feature_enabled": True,
            })
            ws.send(sub_msg)
            log.info(f"WebSocket: subscribed to {len(self.token_ids)} tokens")

            def ping_loop():
                while self._running and self._connected:
                    try:
                        ws.send("PING")
                    except Exception:
                        break
                    _time.sleep(10)

            threading.Thread(target=ping_loop, daemon=True).start()

        def on_message(ws, message):
            if message == "PONG":
                return
            self._last_message_time = _time.time()
            try:
                data = json.loads(message)
                self._process_message(data)
            except (json.JSONDecodeError, KeyError, TypeError):
                log.debug(f"WebSocket: unparseable message: {message[:100]}")

        def on_error(ws, error):
            log.debug(f"WebSocket error: {error}")

        def on_close(ws, close_status_code, close_msg):
            self._connected = False
            log.info("WebSocket: connection closed")

        self._ws = websocket.WebSocketApp(
            config.WS_HOST,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws.run_forever()

    def _update_asset(self, asset_id: str, **fields):
        """Thread-safe update of price state for an asset."""
        now = _time.time()
        with self._lock:
            entry = self._prices.setdefault(asset_id, {"timestamp": now})
            entry["timestamp"] = now
            for k, v in fields.items():
                if v is not None:
                    entry[k] = v

    def _process_message(self, data: dict):
        """
        Parse a WS message and update shared price state.

        Handles documented Polymarket CLOB WS event types:
        - best_bid_ask: best bid/ask update (requires custom_feature_enabled)
        - price_change: order book price level changes, includes best_bid/best_ask
        - book: full order book snapshot
        - last_trade_price: trade execution with price/size
        """
        event_type = data.get("event_type")

        if event_type == "best_bid_ask":
            # {event_type, asset_id, best_bid, best_ask, spread, timestamp}
            asset_id = data.get("asset_id")
            if asset_id:
                self._update_asset(
                    asset_id,
                    best_bid=_safe_float(data.get("best_bid")),
                    best_ask=_safe_float(data.get("best_ask")),
                )

        elif event_type == "price_change":
            # {event_type, price_changes: [{asset_id, price, size, side, best_bid, best_ask}]}
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                if asset_id:
                    self._update_asset(
                        asset_id,
                        best_bid=_safe_float(change.get("best_bid")),
                        best_ask=_safe_float(change.get("best_ask")),
                    )

        elif event_type == "book":
            # {event_type, asset_id, bids: [{price, size}], asks: [{price, size}]}
            asset_id = data.get("asset_id")
            if asset_id:
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                best_bid = _safe_float(bids[0]["price"]) if bids else None
                best_ask = _safe_float(asks[0]["price"]) if asks else None
                self._update_asset(asset_id, best_bid=best_bid, best_ask=best_ask)

        elif event_type == "last_trade_price":
            # {event_type, asset_id, price, side, size, fee_rate_bps}
            asset_id = data.get("asset_id")
            if asset_id:
                self._update_asset(
                    asset_id,
                    last_trade_price=_safe_float(data.get("price")),
                )

        elif event_type in ("tick_size_change", "new_market", "market_resolved"):
            pass  # informational, no price state to update

        else:
            log.debug(f"WebSocket: unknown event_type={event_type!r}")


def _safe_float(val) -> float | None:
    """Convert a string or numeric value to float, or None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
