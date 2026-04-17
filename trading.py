"""
Trading execution — place/cancel orders via py-clob-client.
All operations respect DRY_RUN mode.
"""

from typing import Optional

from py_clob_client.clob_types import OrderArgs, OrderType

import config
from models import Market
from utils import log, format_usd


class TradingClient:
    """
    Wrapper around py-clob-client for placing and managing orders.

    In DRY_RUN mode (default), all actions are logged but never submitted.
    """

    def __init__(self):
        self.dry_run = config.DRY_RUN
        self._client = None
        self._initialized = False

    def initialize(self) -> bool:
        """
        Initialize the CLOB client with API credentials.

        Returns True on success, False on failure.
        """
        if self._initialized:
            return True

        if self.dry_run:
            log.info("🔒 DRY RUN MODE — no real orders will be placed")
            self._initialized = True
            return True

        try:
            from py_clob_client.client import ClobClient

            kwargs = {
                "host": config.CLOB_HOST,
                "key": config.PRIVATE_KEY,
                "chain_id": config.CHAIN_ID,
            }

            # Proxy wallet (Google login) needs funder and signature type
            if config.SIGNATURE_TYPE in (1, 2):
                kwargs["signature_type"] = config.SIGNATURE_TYPE
                kwargs["funder"] = config.FUNDER_ADDRESS

            self._client = ClobClient(**kwargs)

            # Derive API credentials
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)

            log.info("✅ Trading client initialized successfully")
            self._initialized = True
            return True

        except ImportError:
            log.error("py-clob-client not installed. Run: pip install py-clob-client")
            return False
        except Exception as e:
            log.error(f"Failed to initialize trading client: {e}")
            return False

    def place_limit_order(
        self,
        market: Market,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
    ) -> Optional[dict]:
        """
        Place a limit order (GTC — Good 'Til Cancelled).

        Args:
            market: The market to trade on.
            token_id: The specific token (YES or NO) to buy/sell.
            price: Limit price (0.01 to 0.99).
            size: Number of shares.
            side: 'BUY' or 'SELL'.

        Returns:
            Order response dict, or None on failure.
        """
        side_label = "YES" if token_id == market.yes_token_id else "NO"

        if self.dry_run:
            log.info(
                f"🔒 DRY RUN: Would place LIMIT {side} {size:.0f}×{side_label} "
                f"@ {format_usd(price)} on [{market.question[:50]}]"
            )
            return {"dry_run": True, "side": side, "price": price, "size": size}

        if not self._initialized or not self._client:
            log.error("Trading client not initialized")
            return None

        try:
            order = self._client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=side,
                    fee_rate_bps=market.fee_rate_bps if market.fee_rate_bps > 0 else None,
                )
            )
            response = self._client.post_order(order, OrderType.GTC)

            log.info(
                f"✅ LIMIT {side} {size:.0f}×{side_label} @ {format_usd(price)} — "
                f"Order ID: {response.get('orderID', 'unknown')}"
            )
            return response

        except Exception as e:
            log.error(f"❌ Order placement failed: {e}")
            return None

    def place_market_order(
        self,
        market: Market,
        token_id: str,
        amount: float,
        side: str = "BUY",
        worst_price: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Place a market order (FOK — Fill-or-Kill).

        Args:
            market: The market to trade on.
            token_id: The specific token to buy/sell.
            amount: Dollar amount to spend (for BUY).
            side: 'BUY' or 'SELL'.
            worst_price: Maximum price willing to pay (slippage protection).

        Returns:
            Order response dict, or None on failure.
        """
        side_label = "YES" if token_id == market.yes_token_id else "NO"

        if self.dry_run:
            log.info(
                f"🔒 DRY RUN: Would place MARKET {side} {format_usd(amount)} "
                f"of {side_label} (worst: {format_usd(worst_price or 0)}) "
                f"on [{market.question[:50]}]"
            )
            return {"dry_run": True, "side": side, "amount": amount}

        if not self._initialized or not self._client:
            log.error("Trading client not initialized")
            return None

        try:
            market_order = self._client.create_market_order(
                {
                    "tokenID": token_id,
                    "side": side,
                    "amount": amount,
                    "price": worst_price or 0.99,
                },
                {
                    "tickSize": market.tick_size,
                    "negRisk": market.neg_risk,
                },
            )
            response = self._client.post_order(market_order, OrderType.FOK)

            log.info(
                f"✅ MARKET {side} {format_usd(amount)} of {side_label} — "
                f"Order ID: {response.get('orderID', 'unknown')}"
            )
            return response

        except Exception as e:
            log.error(f"❌ Market order failed: {e}")
            return None

    def execute_arbitrage(
        self,
        market: Market,
        size: float,
        yes_price: float,
        no_price: float,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """
        Execute an arbitrage trade — buy both YES and NO.

        Places two limit orders at the current best ask prices.

        Args:
            market: The market to arb.
            size: Number of shares of each side.
            yes_price: Price for YES shares.
            no_price: Price for NO shares.

        Returns:
            Tuple of (yes_response, no_response).
        """
        log.info(f"⚡ Executing arbitrage: {size:.0f} shares each side")

        yes_resp = self.place_limit_order(
            market=market,
            token_id=market.yes_token_id,
            price=yes_price,
            size=size,
            side="BUY",
        )

        no_resp = self.place_limit_order(
            market=market,
            token_id=market.no_token_id,
            price=no_price,
            size=size,
            side="BUY",
        )

        return yes_resp, no_resp

    def cancel_order(self, order_id: str) -> Optional[dict]:
        """Cancel an open order by its ID."""
        if self.dry_run:
            log.info(f"🔒 DRY RUN: Would cancel order {order_id}")
            return {"dry_run": True, "cancelled": order_id}

        if not self._initialized or not self._client:
            log.error("Trading client not initialized")
            return None

        try:
            response = self._client.cancel(order_id)
            log.info(f"✅ Cancelled order {order_id}")
            return response
        except Exception as e:
            log.error(f"❌ Cancel failed for {order_id}: {e}")
            return None

    def cancel_all_orders(self) -> Optional[dict]:
        """Cancel all open orders."""
        if self.dry_run:
            log.info("🔒 DRY RUN: Would cancel all orders")
            return {"dry_run": True}

        if not self._initialized or not self._client:
            log.error("Trading client not initialized")
            return None

        try:
            response = self._client.cancel_all()
            log.info("✅ Cancelled all open orders")
            return response
        except Exception as e:
            log.error(f"❌ Cancel all failed: {e}")
            return None

    def get_open_orders(self) -> list:
        """Get all currently open orders."""
        if self.dry_run:
            return []
        if not self._initialized or not self._client:
            return []
        try:
            return self._client.get_open_orders() or []
        except Exception as e:
            log.debug(f"Failed to fetch open orders: {e}")
            return []

    def get_trades(self) -> list:
        """Get recent trade history."""
        if self.dry_run:
            return []
        if not self._initialized or not self._client:
            return []
        try:
            return self._client.get_trades() or []
        except Exception as e:
            log.debug(f"Failed to fetch trades: {e}")
            return []
