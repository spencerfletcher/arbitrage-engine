"""
scripts/list_markets.py
───────────────────────
Read-only: list live Poly US ↔ Kalshi matched markets with the exact slug +
ticker strings the other scripts want. Copy a row straight into latency_probe /
poly_us_ws_capture / kalshi_orderbook_capture.

  --poly-slug  = the orderable Poly US market-side slug (MarketPair.token_yes_a)
  --kalshi-ticker = the matched Kalshi market ticker (CrossPlatformPair.kalshi_market.ticker)

Makes NO trades. Run:  .venv/bin/python -m scripts.list_markets [--all]
  (default: only matched cross-pairs; --all also dumps every Poly US slug.)
"""
from __future__ import annotations

import argparse
import asyncio

from bot.core import config


async def main() -> None:
    ap = argparse.ArgumentParser(description="List live Poly US / Kalshi markets for testing")
    ap.add_argument("--all", action="store_true",
                    help="also list every Poly US slug (not just matched pairs)")
    args = ap.parse_args()

    from bot.poly_us.scanner import PolyUSScanner
    from bot.kalshi.client import KalshiClient
    from bot.kalshi.scanner import KalshiScanner
    from bot.kalshi.matcher import match_kalshi_events
    from polymarket_us import AsyncPolymarketUS

    sdk = AsyncPolymarketUS(
        key_id=config.POLYMARKET_US_KEY_ID or None,
        secret_key=config.POLYMARKET_US_SECRET_KEY or None,
    )
    kalshi = None
    try:
        poly_pairs = await PolyUSScanner(sdk).fetch_markets(config.POLYMARKET_US_SERIES)

        if not config.KALSHI_API_KEY:
            print("KALSHI_API_KEY not set — can't match. Showing Poly US slugs only.\n")
            _dump_poly(poly_pairs)
            return

        kalshi = KalshiClient()
        kmarkets = []
        for sid in config.KALSHI_SERIES:
            kmarkets.extend(await KalshiScanner(kalshi).fetch_markets([sid]))
        matched = match_kalshi_events(poly_pairs, kmarkets, {})

        print(f"\n── Matched cross-pairs ({len(matched)} of "
              f"{len(poly_pairs)} Poly US / {len(kmarkets)} Kalshi) ──\n")
        for cp in matched:
            slug = cp.poly_pair.token_yes_a       # orderable A-side slug
            ticker = cp.kalshi_market.ticker
            print(f"{cp.poly_pair.event_title}")
            print(f"  poly-slug     : {slug}")
            print(f"  kalshi-ticker : {ticker}")
            print(f"  probe         : .venv/bin/python -m scripts.latency_probe "
                  f"--kalshi-ticker {ticker} --poly-slug {slug}")
            print()

        if args.all:
            _dump_poly(poly_pairs)
    finally:
        await sdk.close()
        if kalshi is not None:
            await kalshi.close()


def _dump_poly(poly_pairs) -> None:
    print(f"── All Poly US slugs ({len(poly_pairs)}) ──")
    for p in poly_pairs:
        print(f"  {p.event_title}")
        print(f"    A: {p.token_yes_a}")
        print(f"    B: {p.token_yes_b}")


if __name__ == "__main__":
    asyncio.run(main())
