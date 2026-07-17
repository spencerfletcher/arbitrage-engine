"""
bot/client.py
─────────────
Thin wrapper around the official py-clob-client that:
  - Handles L1/L2 authentication automatically
  - Exposes DRY_RUN mode (logs orders instead of posting them)
  - Provides helpers used by scanner, arb_detector, and executor
"""
import logging
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs,
    MarketOrderArgs,
    OrderType,
    BookParams,
)
from py_clob_client.order_builder.constants import BUY, SELL

from bot.core import config
from bot.core.logger import get_logger

log = get_logger(__name__)


class PolymarketClient:
    """Authenticated Polymarket CLOB client with dry-run support."""

    def __init__(self) -> None:
        # Ensure wallet credentials are present before attempting auth
        config.validate_trading_config()

        self._client = ClobClient(
            host=config.CLOB_HOST,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=config.SIGNATURE_TYPE,
            funder=config.FUNDER_ADDRESS,
        )
        # Derive L2 API credentials from L1 private key automatically
        try:
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
            log.info("✅ Polymarket CLOB client authenticated")
        except Exception as exc:
            log.warning(f"⚠️  Could not derive API creds: {exc}. Read-only mode only.")

        if config.DRY_RUN:
            log.info("🔍 DRY RUN mode enabled — no real orders will be placed")

    # ── Market data ───────────────────────────────────────────────────────────

    def get_order_book(self, token_id: str):
        """Return full order book for a token."""
        return self._client.get_order_book(token_id)

    def get_order_books(self, token_ids: list[str]):
        """Batch fetch order books."""
        params = [BookParams(token_id=tid) for tid in token_ids]
        return self._client.get_order_books(params)

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Return the current best ask price for a token, or None if illiquid."""
        try:
            price = self._client.get_price(token_id, side="BUY")
            return float(price) if price else None
        except Exception as exc:
            log.debug(f"get_best_ask({token_id}): {exc}")
            return None

    def fetch_best_asks(self, token_ids: list[str], chunk_size: int = 100) -> dict[str, Optional[float]]:
        """Batch-fetch best ask price for many tokens via the CLOB's /books endpoint.

        Used by the cross-arb loop to get guaranteed-fresh prices for matched
        tokens — the WebSocket feed is unreliable for low-volume tokens whose
        cached best_ask can drift far from the real orderbook.

        Returns {token_id: best_ask_float_or_None}. None means either the token
        has no asks (empty book) or the fetch failed.
        """
        from py_clob_client.clob_types import BookParams
        out: dict[str, Optional[float]] = {tid: None for tid in token_ids}
        for i in range(0, len(token_ids), chunk_size):
            chunk = token_ids[i:i + chunk_size]
            params = [BookParams(token_id=tid) for tid in chunk]
            try:
                books = self._client.get_order_books(params)
            except Exception as exc:
                log.debug(f"fetch_best_asks chunk {i}-{i+len(chunk)}: {exc}")
                continue
            for book in books:
                tid = getattr(book, "asset_id", None)
                if not tid:
                    continue
                asks = getattr(book, "asks", None) or []
                if not asks:
                    continue
                # asks are returned sorted ascending by price; best ask = lowest
                try:
                    best = min(float(level.price) for level in asks)
                    out[tid] = best
                except (ValueError, AttributeError):
                    continue
        return out

    def get_available_liquidity(self, token_id: str, max_price: float) -> float:
        """
        Sum shares available in the ask side of the order book up to max_price.
        Includes a small tolerance (+0.01) since Polymarket FOK sweeps all
        asks at-or-below your limit price.
        Returns total shares that can be bought at-or-below max_price.
        """
        try:
            book = self._client.get_order_book(token_id)
            total = 0.0
            limit = max_price + 0.01  # tolerance for rounding
            for level in book.asks:
                if float(level.price) <= limit:
                    total += float(level.size)
            return total
        except Exception as exc:
            log.debug(f"get_available_liquidity({token_id}): {exc}")
            return 0.0

    def get_usdc_balance(self, force_real: bool = False) -> float:
        """
        Fetch USDC collateral balance from the Polymarket CLOB exchange.
        This is the balance available for placing orders — Polymarket deposits
        go to the CTF Exchange contract, not the raw wallet, so raw Polygon
        RPC balance of the EOA would always show $0 after depositing.
        Returns balance in USDC. Returns -1.0 on error.

        In DRY_RUN this returns a 9999 sentinel so the executor's pre-trade check
        always "affords" simulated trades. Pass force_real=True to bypass that and
        fetch the actual balance (e.g. for FBAR/tax logging).
        """
        if config.DRY_RUN and not force_real:
            return 9999.0

        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        try:
            resp = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            raw = resp.get("balance", "0") if isinstance(resp, dict) else "0"
            return float(raw) / 1e6
        except Exception as exc:
            log.error(f"get_usdc_balance: CLOB balance fetch failed: {exc}")
            return -1.0

    # ── Order placement ───────────────────────────────────────────────────────

    def place_limit_fok(
        self, token_id: str, price: float, size: float, label: str = ""
    ) -> Optional[dict]:
        """
        Place a FOK (Fill-or-Kill) limit buy order.

        In DRY_RUN mode this only logs the intended order and returns a
        mock response instead of hitting the API.
        """
        tag = f"[DRY RUN] " if config.DRY_RUN else ""
        log.info(
            f"{tag}ORDER  token={token_id[:8]}…  price={price:.4f}  "
            f"size={size:.2f} shares  cost=${price * size:.2f}  {label}"
        )

        if config.DRY_RUN:
            return {
                "status": "dry_run",
                "token_id": token_id,
                "price": price,
                "size": size,
            }

        try:
            order = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                size=round(size, 2),
                side=BUY,
            )
            signed = self._client.create_order(order)
            resp = self._client.post_order(signed, OrderType.FOK)
            return resp
        except Exception as exc:
            log.error(f"Order placement failed for {token_id}: {exc}")
            return None

    def sell_back(
        self, token_id: str, size: float, label: str = ""
    ) -> Optional[float]:
        """
        Emergency sell of a stranded position at the current best bid.

        Strategy:
          1. Try FOK at the current best bid.
          2. If that fails, try FOK at a slight discount (best_bid - 0.02)
             to aggressively cross the spread and get out.

        Returns the price at which the shares were sold, or None if failed.
        """
        tag = "[DRY RUN] " if config.DRY_RUN else ""
        log.warning(f"{tag}SELL-BACK  token={token_id[:8]}…  size={size:.2f}  {label}")

        if config.DRY_RUN:
            return 0.50  # Mock execution price

        try:
            book = self._client.get_order_book(token_id)
            if not book.bids:
                log.error(f"No bids available to sell-back {token_id}")
                return None
            best_bid = max(float(b.price) for b in book.bids)

            # Try 1: FOK at best bid
            order1 = OrderArgs(
                token_id=token_id,
                price=round(best_bid, 4),
                size=round(size, 2),
                side=SELL,
            )
            try:
                signed1 = self._client.create_order(order1)
                resp1 = self._client.post_order(signed1, OrderType.FOK)
                if resp1:
                    log.info(f"✅ Sell-back succeeded for {token_id[:8]}… at {best_bid}")
                    return best_bid
            except Exception as e:
                log.debug(f"Sell-back FOK at best bid missed: {e}")

            # Try 2: FOK at best bid - $0.02
            discount_bid = max(0.01, best_bid - 0.02)
            log.warning(f"⚠️ Retrying sell-back at discount: {discount_bid}")
            order2 = OrderArgs(
                token_id=token_id,
                price=round(discount_bid, 4),
                size=round(size, 2),
                side=SELL,
            )
            signed2 = self._client.create_order(order2)
            resp2 = self._client.post_order(signed2, OrderType.FOK)
            if resp2:
                log.info(f"✅ Sell-back succeeded for {token_id[:8]}… at discount {discount_bid}")
                return discount_bid

            log.error(f"Sell-back FOK failed for {token_id[:8]}… — position stranded!")
            return None

        except Exception as exc:
            log.error(f"Sell-back error for {token_id}: {exc} — position stranded!")
            return None
