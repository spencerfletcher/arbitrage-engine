"""
scripts/verify_execution.py
───────────────────────────
Live micro-verification of the execution pipeline — proves the real things unit
tests can't: order placement, fill-detection on real response shapes, and
unwind, on each venue. Does ONE 1-unit round-trip (buy then immediately sell to
flatten) per venue, with an aggressive limit so it fills at the resting market
price (the limit is the worst price accepted, not what you pay).

REAL MONEY. Tiny (1 unit each, ~cents of spread+fees). Requires --confirm.
Without --confirm it only prints the plan.

Usage:
  .venv/bin/python -m scripts.verify_execution \
      --kalshi-ticker KXWCGAME-26JUN17AUTJOR-AUT --kalshi-side no \
      --poly-slug atc-fwc-aut-jor-2026-06-17-aut --confirm

Either venue can be skipped by omitting its arg. Get tickers/slugs from the bot
startup log or scripts/kalshi_orders.py / scripts/poly_us_orders.py.
"""
from __future__ import annotations

import argparse
import asyncio

from bot.core import config
from bot.kalshi.client import KalshiClient, kalshi_order_filled
from bot.poly_us.client import PolyUSClient, order_is_filled


async def verify_kalshi(ticker: str, side: str, confirm: bool) -> None:
    print(f"\n=== KALSHI {ticker} [{side}] ===")
    client = KalshiClient()
    if not confirm:
        print("  (dry preview) would BUY 1 @ limit 0.99, then SELL 1 @ limit 0.01")
        return
    # Buy 1 at an aggressive limit → fills at the resting price.
    buy = await client.create_order(ticker, side, "buy", 1, "0.9900")
    print(f"  BUY  resp: {buy!r}")
    if not kalshi_order_filled(buy):
        print("  ❌ FAIL: buy did not fill (check kalshi_order_filled vs response)")
        return
    print("  ✅ buy filled + detected")
    # Flatten: sell 1 at an aggressive-low limit → fills at best bid.
    try:
        sell = await client.create_order(ticker, side, "sell", 1, "0.0100")
        print(f"  SELL resp: {sell!r}")
        if kalshi_order_filled(sell):
            print("  ✅ PASS: round-trip complete, flat")
        else:
            print("  ⚠️  sell not detected as filled — VERIFY you are flat (1 contract may remain)")
    except Exception as exc:
        print(f"  ⚠️  SELL FAILED: {exc} — 1 {side} contract may remain, flatten manually")


async def verify_poly(slug: str, confirm: bool) -> None:
    print(f"\n=== POLY US {slug} ===")
    client = PolyUSClient()
    try:
        ask = await client.get_best_ask(slug)
        print(f"  best ask: {ask}")
        if not confirm:
            print("  (dry preview) would BUY 1 @ limit 0.99, then sell_back to flatten")
            return
        buy = await client.place_limit_fok(slug, 0.99, 1.0, "[VERIFY]")
        print(f"  BUY  resp: {buy!r}")
        if not order_is_filled(buy):
            print("  ❌ FAIL: buy did not fill (check order_is_filled / synchronousExecution)")
            return
        print("  ✅ buy filled + detected")
        # sell_back → (vwap_price, sold_qty): Poly rewrites FOK→IOC so a sell can partial-fill.
        sold_px, sold_qty = await client.sell_back(slug, 1.0, "[VERIFY]")
        if sold_qty >= 1:
            print(f"  ✅ PASS: round-trip complete, flat (sold {sold_qty:.0f} @ {sold_px})")
        else:
            print(f"  ⚠️  sell_back sold {sold_qty:.0f}/1 — VERIFY you are flat "
                  f"(1 share may remain)")
    finally:
        await client.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kalshi-ticker")
    ap.add_argument("--kalshi-side", default="no", choices=["yes", "no"])
    ap.add_argument("--poly-slug")
    ap.add_argument("--confirm", action="store_true", help="place REAL 1-unit orders")
    args = ap.parse_args()

    if config.DRY_RUN and args.confirm:
        print("DRY_RUN=true in .env — orders will be simulated, not real. "
              "Set DRY_RUN=false to truly verify.")
    if not args.confirm:
        print("PREVIEW ONLY (no --confirm). No orders will be placed.")

    if args.kalshi_ticker:
        await verify_kalshi(args.kalshi_ticker, args.kalshi_side, args.confirm)
    if args.poly_slug:
        await verify_poly(args.poly_slug, args.confirm)
    if not args.kalshi_ticker and not args.poly_slug:
        print("Nothing to do — pass --kalshi-ticker and/or --poly-slug.")


if __name__ == "__main__":
    asyncio.run(main())
