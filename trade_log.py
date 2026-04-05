"""
Trade logging — persist detected arbitrage opportunities and executed trades to CSV.
"""

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from models import ArbitrageOpportunity

LOG_DIR = Path(__file__).parent / "logs"
ARB_LOG = LOG_DIR / "arb_opportunities.csv"
EXEC_LOG = LOG_DIR / "executions.csv"

ARB_FIELDS = [
    "timestamp", "market_question", "yes_price", "no_price", "combined_cost",
    "fee_rate_bps", "total_fees", "gross_spread", "net_profit", "roi_pct",
    "is_profitable", "yes_liquidity", "no_liquidity", "max_profitable_size",
]

EXEC_FIELDS = [
    "timestamp", "market_question", "condition_id", "side", "size",
    "yes_price", "no_price", "net_profit", "roi_pct", "status",
]


def _ensure_csv(path: Path, fields: list[str]):
    """Create the CSV file with headers if it doesn't exist."""
    LOG_DIR.mkdir(exist_ok=True)
    if not path.exists():
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def log_arb_opportunity(opp: ArbitrageOpportunity):
    """Append an arbitrage opportunity to the CSV log."""
    _ensure_csv(ARB_LOG, ARB_FIELDS)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_question": opp.market_question,
        "yes_price": f"{opp.yes_price:.6f}",
        "no_price": f"{opp.no_price:.6f}",
        "combined_cost": f"{opp.combined_cost:.6f}",
        "fee_rate_bps": opp.fee_rate_bps,
        "total_fees": f"{opp.total_fees:.6f}",
        "gross_spread": f"{opp.gross_spread:.6f}",
        "net_profit": f"{opp.net_profit:.6f}",
        "roi_pct": f"{opp.roi_pct:.4f}",
        "is_profitable": opp.is_profitable,
        "yes_liquidity": f"{opp.yes_liquidity:.0f}",
        "no_liquidity": f"{opp.no_liquidity:.0f}",
        "max_profitable_size": f"{opp.max_profitable_size:.0f}",
    }
    with open(ARB_LOG, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=ARB_FIELDS).writerow(row)


def log_execution(
    market_question: str,
    condition_id: str,
    size: float,
    yes_price: float,
    no_price: float,
    net_profit: float,
    roi_pct: float,
    status: str,
):
    """Append a trade execution to the CSV log."""
    _ensure_csv(EXEC_LOG, EXEC_FIELDS)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_question": market_question,
        "condition_id": condition_id,
        "side": "ARB_BOTH",
        "size": f"{size:.0f}",
        "yes_price": f"{yes_price:.6f}",
        "no_price": f"{no_price:.6f}",
        "net_profit": f"{net_profit:.6f}",
        "roi_pct": f"{roi_pct:.4f}",
        "status": status,
    }
    with open(EXEC_LOG, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=EXEC_FIELDS).writerow(row)
