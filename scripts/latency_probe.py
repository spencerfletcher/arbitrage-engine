"""
scripts/latency_probe.py
────────────────────────
Measure real round-trip latency to each venue (read-only). Answers "is Poly ~10ms
or ~90ms?" with data, to decide sequential vs concurrent leg execution.

Times warm REST round-trips (skips the first to exclude TLS handshake):
  - Kalshi:  GET /markets/{ticker}/orderbook  (uses the pooled keep-alive session)
  - Poly US: markets.bbo(slug)

Run: .venv/bin/python -m scripts.latency_probe --kalshi-ticker KXWCGAME-... --poly-slug atc-...
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from polymarket_us import AsyncPolymarketUS

from bot.core import config
from bot.kalshi.client import KalshiClient


async def _time(coro_fn, n=11):
    ts = []
    for i in range(n):
        t0 = time.perf_counter()
        try:
            await coro_fn()
        except Exception as e:
            print(f"  call {i} failed: {e!r}")
            continue
        ts.append((time.perf_counter() - t0) * 1000)
    ts = ts[1:]  # drop first (handshake / warmup)
    return ts


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kalshi-ticker", required=True)
    ap.add_argument("--poly-slug", required=True)
    args = ap.parse_args()

    k = KalshiClient()
    kt = await _time(lambda: k.get_orderbook(args.kalshi_ticker, depth=1))
    if kt:
        print(f"Kalshi RTT (warm): median {statistics.median(kt):.0f}ms  "
              f"min {min(kt):.0f}  max {max(kt):.0f}  (n={len(kt)})")

    sdk = AsyncPolymarketUS(key_id=config.POLYMARKET_US_KEY_ID,
                            secret_key=config.POLYMARKET_US_SECRET_KEY)
    try:
        pt = await _time(lambda: sdk.markets.bbo(args.poly_slug))
        if pt:
            print(f"Poly RTT  (warm): median {statistics.median(pt):.0f}ms  "
                  f"min {min(pt):.0f}  max {max(pt):.0f}  (n={len(pt)})")
    finally:
        await sdk.close()
    await k.close()


if __name__ == "__main__":
    asyncio.run(main())
