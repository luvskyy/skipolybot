"""
Arbitrage detection engine.

Detects when buying YES + NO on a binary market costs less than $1.00,
accounting for Polymarket's sliding-scale taker fees.
"""

from models import ArbitrageOpportunity, Market, OrderBook, PriceSnapshot
from market_data import compute_fill_price
from utils import log, format_usd, format_pct
import config


# ── Fee Calculation ──────────────────────────────────────────────────────────

def calculate_fee(shares: float, price: float, fee_rate_bps: int) -> float:
    """
    Calculate the Polymarket taker fee for a trade.

    Formula: fee = C × feeRate × p × (1 - p)
    Where:
        C = number of shares
        feeRate = fee_rate_bps / 10000
        p = price per share (0 to 1)

    The fee is highest at p=0.50 (maximum uncertainty) and drops
    toward zero as p approaches 0 or 1.

    Args:
        shares: Number of shares being bought.
        price: Price per share (0.0 to 1.0).
        fee_rate_bps: Fee rate in basis points (e.g. 300 = 3%).

    Returns:
        Fee amount in USDC.
    """
    if fee_rate_bps <= 0 or price <= 0 or price >= 1 or shares <= 0:
        return 0.0
    fee_rate = fee_rate_bps / 10000.0
    return shares * fee_rate * price * (1.0 - price)


def calculate_fee_for_dollar_amount(dollar_amount: float, price: float, fee_rate_bps: int) -> float:
    """
    Calculate fee when spending a fixed dollar amount.

    shares = dollar_amount / price
    fee = shares × feeRate × p × (1 - p)
    """
    if price <= 0:
        return 0.0
    shares = dollar_amount / price
    return calculate_fee(shares, price, fee_rate_bps)


# ── Core Arbitrage Detection ────────────────────────────────────────────────

def detect_arbitrage(
    market: Market,
    prices: PriceSnapshot,
    shares: float = 1.0,
) -> ArbitrageOpportunity:
    """
    Detect if an arbitrage opportunity exists on a binary market.

    The logic:
    1. Buy `shares` of YES at the best ask price
    2. Buy `shares` of NO at the best ask price
    3. Combined cost = (yes_ask × shares) + (no_ask × shares)
    4. Guaranteed payout = 1.0 × shares (one side always resolves to $1)
    5. Fees are applied to each side
    6. Net profit = payout - combined_cost - total_fees

    Args:
        market: The market being analyzed.
        prices: Current price snapshot.
        shares: Number of shares to analyze (default: 1).

    Returns:
        ArbitrageOpportunity with full breakdown.
    """
    yes_price = prices.yes_ask or 0.0
    no_price = prices.no_ask or 0.0

    # Combined cost to buy both sides
    combined_cost = yes_price + no_price

    # Fee calculation
    fee_rate = market.fee_rate_bps
    fee_yes = calculate_fee(shares, yes_price, fee_rate)
    fee_no = calculate_fee(shares, no_price, fee_rate)
    total_fees = fee_yes + fee_no

    # Profit calculation
    payout = 1.0 * shares
    gross_spread = payout - (combined_cost * shares)
    net_profit = gross_spread - total_fees

    # ROI
    total_investment = (combined_cost * shares) + total_fees
    roi_pct = (net_profit / total_investment * 100) if total_investment > 0 else 0.0

    is_profitable = net_profit > 0

    return ArbitrageOpportunity(
        market_question=market.question,
        yes_price=yes_price,
        no_price=no_price,
        combined_cost=combined_cost,
        fee_rate_bps=fee_rate,
        fee_yes=fee_yes,
        fee_no=fee_no,
        total_fees=total_fees,
        gross_spread=1.0 - combined_cost,
        net_profit=net_profit,
        roi_pct=roi_pct,
        is_profitable=is_profitable,
    )


def detect_arbitrage_with_depth(
    market: Market,
    yes_book: OrderBook,
    no_book: OrderBook,
    target_size: float = 100.0,
) -> ArbitrageOpportunity:
    """
    Advanced arbitrage detection that accounts for order book depth and slippage.

    Instead of just checking the best ask, this walks the order book to compute
    the actual volume-weighted average fill price for a target position size.

    Args:
        market: The market being analyzed.
        yes_book: Order book for the YES token.
        no_book: Order book for the NO token.
        target_size: Number of shares to analyze.

    Returns:
        ArbitrageOpportunity with slippage-adjusted numbers.
    """
    # Compute VWAP fill prices
    yes_fill = compute_fill_price(yes_book.asks, target_size)
    no_fill = compute_fill_price(no_book.asks, target_size)

    if yes_fill is None or no_fill is None:
        # Not enough liquidity for the target size — use best ask as fallback
        yes_fill = yes_book.best_ask or 0.0
        no_fill = no_book.best_ask or 0.0
        effective_size = min(
            yes_book.asks.best_size or 0,
            no_book.asks.best_size or 0,
        )
    else:
        effective_size = target_size

    combined_cost = yes_fill + no_fill
    fee_rate = market.fee_rate_bps
    fee_yes = calculate_fee(effective_size, yes_fill, fee_rate)
    fee_no = calculate_fee(effective_size, no_fill, fee_rate)
    total_fees = fee_yes + fee_no

    payout = 1.0 * effective_size
    gross_spread = payout - (combined_cost * effective_size)
    net_profit = gross_spread - total_fees

    total_investment = (combined_cost * effective_size) + total_fees
    roi_pct = (net_profit / total_investment * 100) if total_investment > 0 else 0.0

    return ArbitrageOpportunity(
        market_question=market.question,
        yes_price=yes_fill,
        no_price=no_fill,
        combined_cost=combined_cost,
        fee_rate_bps=fee_rate,
        fee_yes=fee_yes,
        fee_no=fee_no,
        total_fees=total_fees,
        gross_spread=1.0 - combined_cost,
        net_profit=net_profit,
        roi_pct=roi_pct,
        is_profitable=net_profit > 0,
        yes_liquidity=yes_book.asks.best_size or 0,
        no_liquidity=no_book.asks.best_size or 0,
        max_profitable_size=effective_size,
    )


def find_max_profitable_size(
    market: Market,
    yes_book: OrderBook,
    no_book: OrderBook,
    max_size: float = 1000.0,
    step: float = 10.0,
) -> tuple[float, float]:
    """
    Find the maximum position size that remains profitable after slippage and fees.

    Walks up from 1 share in increments, checking profitability at each level.

    Returns:
        (max_profitable_size, net_profit_at_that_size)
    """
    best_size = 0.0
    best_profit = 0.0

    size = step
    while size <= max_size:
        yes_fill = compute_fill_price(yes_book.asks, size)
        no_fill = compute_fill_price(no_book.asks, size)

        if yes_fill is None or no_fill is None:
            break  # Ran out of liquidity

        combined = yes_fill + no_fill
        fee_rate = market.fee_rate_bps
        total_fees = (
            calculate_fee(size, yes_fill, fee_rate) +
            calculate_fee(size, no_fill, fee_rate)
        )

        payout = 1.0 * size
        net_profit = payout - (combined * size) - total_fees

        if net_profit > 0:
            best_size = size
            best_profit = net_profit
        else:
            break  # Past the profitable point

        size += step

    return best_size, best_profit


# ── Logging / Display ────────────────────────────────────────────────────────

def log_opportunity(opp: ArbitrageOpportunity) -> None:
    """Log a detected arbitrage opportunity."""
    if opp.is_profitable:
        log.info("🟢 ARBITRAGE OPPORTUNITY DETECTED!")
        log.info(f"   Market:        {opp.market_question}")
        log.info(f"   YES ask:       {format_usd(opp.yes_price)}")
        log.info(f"   NO  ask:       {format_usd(opp.no_price)}")
        log.info(f"   Combined:      {format_usd(opp.combined_cost)}")
        log.info(f"   Gross spread:  {format_usd(opp.gross_spread)}")
        log.info(f"   Fees:          {format_usd(opp.total_fees)} ({opp.fee_rate_bps} bps)")
        log.info(f"   Net profit:    {format_usd(opp.net_profit)}")
        log.info(f"   ROI:           {format_pct(opp.roi_pct)}")
        if opp.yes_liquidity > 0:
            log.info(f"   YES liquidity: {opp.yes_liquidity:.0f} shares")
            log.info(f"   NO  liquidity: {opp.no_liquidity:.0f} shares")
    else:
        log.debug(
            f"No arb: combined={format_usd(opp.combined_cost)}, "
            f"spread={format_usd(opp.gross_spread)}, "
            f"fees={format_usd(opp.total_fees)}, "
            f"net={format_usd(opp.net_profit)}"
        )
