"""
Data models used throughout the bot.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Market:
    """Represents a single 15-min BTC market on Polymarket."""
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_date: Optional[datetime]
    active: bool
    neg_risk: bool
    tick_size: str
    fee_rate_bps: int = 0
    market_id: str = ""        # Gamma API market id
    group_id: str = ""         # parent group for recurring markets
    description: str = ""

    @property
    def is_expired(self) -> bool:
        if self.end_date is None:
            return False
        return datetime.now(timezone.utc) >= self.end_date

    @property
    def time_remaining(self) -> Optional[float]:
        """Seconds until market resolves, or None if no end date."""
        if self.end_date is None:
            return None
        delta = self.end_date - datetime.now(timezone.utc)
        return max(delta.total_seconds(), 0)


@dataclass
class OrderBookLevel:
    """A single price level in the order book."""
    price: float
    size: float


@dataclass
class OrderBookSide:
    """One side (bids or asks) of an order book."""
    levels: list[OrderBookLevel] = field(default_factory=list)

    @property
    def best(self) -> Optional[float]:
        if not self.levels:
            return None
        return self.levels[0].price

    @property
    def best_size(self) -> Optional[float]:
        if not self.levels:
            return None
        return self.levels[0].size

    def depth_at_price(self, price: float) -> float:
        """Total size available at or better than the given price (for asks: <=, for bids: >=)."""
        return sum(lvl.size for lvl in self.levels if lvl.price <= price)


@dataclass
class OrderBook:
    """Full order book for a single token (YES or NO)."""
    token_id: str
    bids: OrderBookSide = field(default_factory=OrderBookSide)
    asks: OrderBookSide = field(default_factory=OrderBookSide)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids.best

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks.best

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class PriceSnapshot:
    """Current prices for both sides of a market."""
    timestamp: datetime
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None

    @property
    def combined_ask(self) -> Optional[float]:
        """Cost to buy 1 share of YES + 1 share of NO."""
        if self.yes_ask is not None and self.no_ask is not None:
            return self.yes_ask + self.no_ask
        return None

    @property
    def combined_bid(self) -> Optional[float]:
        """Value if you could sell 1 share of YES + 1 share of NO."""
        if self.yes_bid is not None and self.no_bid is not None:
            return self.yes_bid + self.no_bid
        return None


@dataclass
class ArbitrageOpportunity:
    """A detected arbitrage opportunity with full breakdown."""
    market_question: str
    yes_price: float          # best ask for YES
    no_price: float           # best ask for NO
    combined_cost: float      # yes_price + no_price
    fee_rate_bps: int         # fee rate in basis points
    fee_yes: float            # calculated fee for YES purchase
    fee_no: float             # calculated fee for NO purchase
    total_fees: float         # fee_yes + fee_no
    gross_spread: float       # 1.0 - combined_cost
    net_profit: float         # gross_spread - total_fees
    roi_pct: float            # net_profit / combined_cost * 100
    is_profitable: bool       # net_profit > 0
    yes_liquidity: float = 0  # shares available at best ask (YES)
    no_liquidity: float = 0   # shares available at best ask (NO)
    max_profitable_size: float = 0  # max shares profitable after slippage
