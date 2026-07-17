"""
scripts/kalshi_quote_backing.py — probe: is the Kalshi top-of-book QUOTE backed by orderbook DEPTH?

Tests the ticker-phantom hypothesis (subsecond_calibration §C / PROJECT_STATE: 17/17 kalshi_moved had
kalshi_fillable=0 — the KALSHI_PRICE_SOURCE=ticker quote prices edges the Kalshi REST orderbook doesn't
back) across ALL currently-live Kalshi markets in ONE shot, instead of waiting days for fill_success to
dribble in. This is the escalate-or-kill step for the orderbook-vs-ticker TODO.

For each live market + side: take the quoted ask (REST market `yes_ask`, and `no_ask = 1 − yes_bid` — the
top-of-book the detector prices on) and compute `_fillable_from_book` at that ask from a FRESH
`get_orderbook` read — the EXACT fire-path function. A quote with ~0 orderbook fillable at its own ask is a
PHANTOM (the price isn't backed by resting size). Reports the per-market table + the phantom rate by side.

READ-ONLY: market list + orderbook GETs, NO orders, no capital. The bot makes these same reads continuously.
CONSERVATIVE: uses the REST market quote as the ticker proxy — the live WS ticker can be STALER, so the real
phantom rate is ≥ what this shows. Snapshot in time: confirms the MECHANISM; it does not replace watching the
§C fraction across sports/days.

Run:  .venv/bin/python -m scripts.kalshi_quote_backing
"""
from __future__ import annotations

import asyncio

from bot.core import config
from bot.kalshi.client import KalshiClient
from bot.kalshi.scanner import KalshiScanner
from bot.runner.kalshi_arb import _fillable_from_book   # the EXACT fire-path fillable fn

_SAMPLE_CAP = 60          # cap live orderbook reads (politeness to the shared rate limit); logged, not silent
_PHANTOM_MAX_FILL = 1.0   # fillable < 1 contract at the quoted ask == phantom (essentially zero)


async def main() -> None:
    client = KalshiClient()
    scanner = KalshiScanner(client)
    markets = await scanner.fetch_markets(config.KALSHI_SERIES)
    # Only markets that actually carry a two-sided quote to test.
    quoted = [m for m in markets if 0.0 < m.yes_ask < 1.0 and 0.0 < m.yes_bid < 1.0]
    sample = quoted[:_SAMPLE_CAP]
    print(f"{len(markets)} live markets, {len(quoted)} two-sided-quoted; probing {len(sample)} "
          f"(cap {_SAMPLE_CAP}{' — TRUNCATED, rest not probed' if len(quoted) > _SAMPLE_CAP else ''})\n")

    rows = []   # (series, ticker, side, quoted_ask, fillable, phantom)
    for m in sample:
        try:
            book = await client.get_orderbook(m.ticker, depth=20)
        except Exception as exc:
            print(f"  orderbook read failed {m.ticker}: {exc!r} — skipped")
            continue
        for side, ask in (("yes", round(m.yes_ask, 4)), ("no", round(1.0 - m.yes_bid, 4))):
            if not (0.0 < ask < 1.0):
                continue
            fillable = _fillable_from_book(book, side, ask)
            rows.append((m.ticker.split("-")[0], m.ticker, side, ask, fillable,
                         fillable < _PHANTOM_MAX_FILL))
        await asyncio.sleep(0.05)   # gentle pacing vs the shared rate limit

    if not rows:
        print("No quotes to evaluate (no live two-sided markets right now?).")
        await client.close() if hasattr(client, "close") else None
        return

    print(f"  {'series':>11} {'side':>4} {'q_ask':>6} {'ob_fillable':>11}  verdict")
    for series, ticker, side, ask, fill, phantom in rows:
        print(f"  {series:>11} {side:>4} {ask:>6.2f} {fill:>11.0f}  {'PHANTOM (unbacked)' if phantom else 'backed'}")

    # summary, by side + series
    from collections import Counter
    tot = Counter(); ph = Counter()
    for series, ticker, side, ask, fill, phantom in rows:
        tot[("side", side)] += 1; tot[("series", series)] += 1; tot["all"] += 1
        if phantom:
            ph[("side", side)] += 1; ph[("series", series)] += 1; ph["all"] += 1
    print(f"\nPHANTOM RATE (quoted ask with <1 contract of orderbook backing):")
    print(f"  overall: {ph['all']}/{tot['all']} = {100*ph['all']//max(tot['all'],1)}%")
    for s in ("yes", "no"):
        if tot[("side", s)]:
            print(f"  side={s}: {ph[('side',s)]}/{tot[('side',s)]} = {100*ph[('side',s)]//tot[('side',s)]}%")
    for k in sorted({series for series, *_ in rows}):
        if tot[("series", k)]:
            print(f"  {k}: {ph[('series',k)]}/{tot[('series',k)]} = {100*ph[('series',k)]//tot[('series',k)]}%")
    print("\nREAD: a high phantom rate here CONFIRMS the mechanism (ticker quote not backed by the book) "
          "live + across sports → the orderbook-vs-ticker TODO escalates. A LOW rate means the REST quote IS "
          "backed, so the §C phantoms come from WS-ticker staleness specifically (still orderbook-ward, "
          "different sub-mechanism). Conservative: real rate ≥ this (WS ticker ≥ as stale as the REST quote).")

    if hasattr(client, "close"):
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
