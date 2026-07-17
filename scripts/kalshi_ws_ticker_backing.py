"""
scripts/kalshi_ws_ticker_backing.py — DIRECT measurement of the stale-WS-ticker phantom hypothesis.

The REST probe (kalshi_quote_backing.py) proved the REST market quote IS backed by the orderbook
(0/120 phantom) — but detection prices off the WS `ticker` channel, which that probe never read, so
"the phantom is a stale WS ticker" stayed an INFERENCE. This reads the WS ticker channel directly and
asks the same question of it: does the WS ticker ask have orderbook depth behind it? If the WS ticker
quotes asks the orderbook doesn't back (while the REST quote did), the `KALSHI_PRICE_SOURCE=ticker`
feed is stale relative to the book — the §C phantom mechanism MEASURED, not inferred. If the WS ticker
is also ~fully backed, stale-feed is REFUTED and the §C phantom comes from elsewhere (limit derivation
/ the specific fast moments). (feed.py:70 already asserts the orderbook mode exists to "kill the phantom
edges the lagging ticker quote produces" — this measures whether that's what's happening.)

READ-ONLY: a separate WS `ticker` subscription + orderbook GETs, NO orders. Reuses the fire-path
`_fillable_from_book`. The WS quote used is the LATEST-seen per ticker (a frozen/stale ticker keeps an
old quote — exactly what we're testing), the orderbook is fetched right after capture (small timing gap;
a persistently-stale ticker shows regardless). Snapshot in time: confirms the mechanism live.

Run:  .venv/bin/python -m scripts.kalshi_ws_ticker_backing [capture_seconds]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets

from bot.core import config
from bot.kalshi.client import KalshiClient
from bot.runner.kalshi_arb import _fillable_from_book   # the EXACT fire-path fillable fn

_SAMPLE_CAP = 40
_PHANTOM_MAX_FILL = 1.0   # <1 contract of orderbook backing at the quoted ask == phantom
_DIV_TOL = 0.01           # |rest_best_ask - ws_ask| <= this == the ticker agrees with the book


def _best_ask_from_book(book: dict, side: str) -> tuple[float | None, float]:
    """The REST book's BEST ask for `side`, and the qty resting at it.

    Same convention as _fillable_from_book: buying `side` lifts the OPPOSING book's bids — a bid at
    p on the other side is an offer at (1-p) — so the best ask is 1 - (highest opposing bid).

    This is the piece the original probe never read. It only asked "is the ticker's ask backed?"
    (the stale-LOW direction, which manufactures phantom arbs). Comparing the ticker's ask to the
    book's BEST ask also exposes the stale-HIGH direction: a book CHEAPER than the ticker means the
    true edge is LARGER than detection believes, so a real arb can be hidden below KALSHI_ARB_MIN_EDGE
    and never logged at all. That population is invisible to every existing log by construction."""
    ob = book.get("orderbook_fp", {}) if isinstance(book, dict) else {}
    levels = ob.get("yes_dollars" if side == "no" else "no_dollars") or []
    best_px, best_qty = None, 0.0
    for lvl in levels:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if best_px is None or px > best_px:
            best_px, best_qty = px, qty
    if best_px is None:
        return None, 0.0
    return round(1.0 - best_px, 4), best_qty


async def _capture_ticker_quotes(client: KalshiClient, series: tuple[str, ...], secs: float) -> dict:
    """Latest WS ticker (yes_bid, yes_ask) per relevant market over `secs` seconds."""
    quotes: dict[str, tuple[float, float]] = {}
    async with websockets.connect(
        client.ws_url, additional_headers=client.ws_headers(),
        ping_interval=20, ping_timeout=20,
    ) as ws:
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"]}}))
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if data.get("type") != "ticker":
                continue
            msg = data.get("msg", {}) if isinstance(data.get("msg"), dict) else {}
            tk = msg.get("market_ticker", "")
            if not tk or tk.split("-")[0] not in series:
                continue
            yb, ya = msg.get("yes_bid_dollars"), msg.get("yes_ask_dollars")
            if yb is None or ya is None:
                continue
            quotes[tk] = (float(yb), float(ya))
    return quotes


async def main() -> None:
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    client = KalshiClient()
    series = tuple(config.KALSHI_SERIES)

    print(f"capturing WS ticker quotes for {secs:.0f}s (series {list(series)})...")
    quotes = await _capture_ticker_quotes(client, series, secs)
    quoted = {tk: q for tk, q in quotes.items() if 0.0 < q[1] < 1.0 and 0.0 < q[0] < 1.0}
    sample = list(quoted.items())[:_SAMPLE_CAP]
    print(f"{len(quotes)} tickers seen, {len(quoted)} two-sided-quoted; probing {len(sample)} "
          f"(cap {_SAMPLE_CAP}{' — TRUNCATED' if len(quoted) > _SAMPLE_CAP else ''})\n")
    if not sample:
        print("No live two-sided ticker quotes captured (quiet window? try a longer capture or during games).")
        return

    rows = []   # (series, side, ws_ask, ob_fillable, phantom, rest_best_ask, rest_qty, ticker)
    for tk, (yb, ya) in sample:
        try:
            book = await client.get_orderbook(tk, depth=20)
        except Exception as exc:
            print(f"  orderbook read failed {tk}: {exc!r} — skipped")
            continue
        for side, ask in (("yes", round(ya, 4)), ("no", round(1.0 - yb, 4))):
            if not (0.0 < ask < 1.0):
                continue
            fill = _fillable_from_book(book, side, ask)
            best, qty = _best_ask_from_book(book, side)
            rows.append((tk.split("-")[0], side, ask, fill, fill < _PHANTOM_MAX_FILL, best, qty, tk))
        await asyncio.sleep(0.05)

    print(f"  {'series':>11} {'side':>4} {'ws_ask':>6} {'ob_fillable':>11} {'rest_ask':>8} {'div':>7}  verdict")
    for s, side, ask, fill, ph, best, qty, _tk in rows:
        div = (best - ask) if best is not None else None
        ds = f"{div:+7.3f}" if div is not None else "      -"
        bs = f"{best:8.2f}" if best is not None else "       -"
        print(f"  {s:>11} {side:>4} {ask:>6.2f} {fill:>11.0f} {bs} {ds}  "
              f"{'PHANTOM (unbacked)' if ph else 'backed'}")

    # ── SIGNED divergence: the direction the original probe never measured ──────────────
    div_rows = [(s, side, ask, best, qty, tk) for s, side, ask, _f, _p, best, qty, tk in rows
                if best is not None]
    agree = [r for r in div_rows if abs(r[3] - r[2]) <= _DIV_TOL]
    stale_low = [r for r in div_rows if r[3] - r[2] > _DIV_TOL]     # book dearer than ticker
    stale_high = [r for r in div_rows if r[3] - r[2] < -_DIV_TOL]   # book CHEAPER -> hidden edge
    n = len(div_rows)
    print(f"\nSIGNED TICKER-vs-BOOK DIVERGENCE on RANDOM live markets (n={n})")
    print("  (random = NOT detection-selected, so this is free of the selection effect that")
    print("   contaminates fat_spike; it is the honest base rate.)")
    if n:
        print(f"  ticker AGREES with book (<={_DIV_TOL:.2f})   : {len(agree):3}/{n} ({100*len(agree)/n:.0f}%)")
        print(f"  ticker STALE-LOW  (book DEARER)      : {len(stale_low):3}/{n} ({100*len(stale_low)/n:.0f}%)"
              "  -> manufactures phantom arbs (§C)")
        print(f"  ticker STALE-HIGH (book CHEAPER)     : {len(stale_high):3}/{n} ({100*len(stale_high)/n:.0f}%)"
              "  -> HIDES real arbs (never logged)")
    if stale_high:
        mags = sorted(r[2] - r[3] for r in stale_high)
        print(f"\n  HIDDEN-EDGE sizing — how much cheaper the real book is, and the size behind it:")
        print(f"    cheaper-by: median {mags[len(mags)//2]:.3f}  max {mags[-1]:.3f}")
        print(f"    {'series':>11} {'side':>4} {'ticker':>7} {'REST':>7} {'cheaper':>8} {'qty@REST':>9}")
        for s, side, ask, best, qty, tk in sorted(stale_high, key=lambda r: -(r[2] - r[3]))[:10]:
            print(f"    {s:>11} {side:>4} {ask:>7.3f} {best:>7.3f} {ask-best:>8.3f} {qty:>9.0f}")
        real = [r for r in stale_high if (r[2] - r[3]) >= 0.02 and r[4] >= 10]
        print(f"\n    materially cheaper (>=2c) AND >=10 contracts resting: {len(real)}/{n} "
              f"({100*len(real)/n:.1f}% of samples)")
        print("    ^ THIS is the number that decides the orderbook flip: edges detection cannot see")
        print("      today, that orderbook mode WOULD see, with real size behind them.")
    else:
        print("\n  No stale-HIGH samples: the book is never cheaper than the ticker in this window ->")
        print("  no hidden-edge upside from flipping; the June 'cleanliness not capture' read HOLDS.")

    from collections import Counter
    tot, ph = Counter(), Counter()
    for s, side, ask, fill, phantom, _b, _q, _tk in rows:
        tot["all"] += 1; tot[("side", side)] += 1
        if phantom:
            ph["all"] += 1; ph[("side", side)] += 1
    rate = 100 * ph["all"] // max(tot["all"], 1)
    print(f"\nWS-TICKER PHANTOM RATE (WS ticker ask with <1 contract of orderbook backing):")
    print(f"  overall: {ph['all']}/{tot['all']} = {rate}%")
    for s in ("yes", "no"):
        if tot[("side", s)]:
            print(f"  side={s}: {ph[('side',s)]}/{tot[('side',s)]} = {100*ph[('side',s)]//tot[('side',s)]}%")
    print("\nVERDICT vs the REST probe (kalshi_quote_backing: REST quote 0/120 backed):")
    if ph["all"] > 0:
        print("  WS-ticker phantom rate > 0 while the REST quote was 0/120 → the WS ticker quotes asks the")
        print("  orderbook doesn't back, but the REST quote doesn't → STALE-WS-TICKER CONFIRMED (measured, not")
        print("  inferred). The §C phantom is a stale detection price feed; the orderbook-pricing / WS-freshness")
        print("  lever (TODO) is the fix, and capturability is not refuted by the low fill rate.")
    else:
        print("  WS-ticker phantom rate ≈ 0 (like the REST quote) → the WS ticker IS backed too → STALE-FEED")
        print("  REFUTED. The §C phantom is NOT the ticker being stale vs the book; look elsewhere (limit")
        print("  derivation, or the specific fast moments §C caught). Re-examine before the orderbook TODO.")
    print("  (Snapshot in time + small capture→orderbook timing gap; not a multi-day result.)")


if __name__ == "__main__":
    asyncio.run(main())
