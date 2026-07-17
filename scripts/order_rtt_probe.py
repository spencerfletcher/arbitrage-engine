"""
scripts/order_rtt_probe.py
──────────────────────────
Measure the ORDER PLACEMENT round-trip — the place→(matching engine)→response time — for each
venue. This is the number NO other script captures: `latency_probe.py` times READS, `verify_*`
place orders but don't time them. It is the calibration target for KALSHI_INTERLEG_PROBE_DELAY_MS
(the §5 inter-leg window ≈ the poly-order place→fill RTT).

HOW IT STAYS ZERO-COST
──────────────────────
It sends FILL-OR-KILL BUYs at an impossible price (default 1¢). No tradeable team's ask is ever 1¢,
so the order routes through the matching engine, fills ZERO, and KILLS — a real place→engine→response
round-trip at zero capital. Every response is asserted zero-fill; on ANY fill the script ALARMS,
attempts to flatten, and aborts.

KALSHI V2 (migrated 2026-07-14 — this changed how a kill LOOKS)
  A killed FOK now raises **409 `fill_or_kill_insufficient_resting_volume`** instead of returning a
  zero-fill body with status='canceled', and the create response is FLAT (no `order`, no `status`).
  The 409 IS the engine's answer — the order traversed the matching engine and was killed there — so
  it is timed as a VALID sample (`status=killed(409)`). Any OTHER exception is a real failure and
  stops that venue. Before this was handled, the first kill hit `except → break` and the probe
  collected ZERO samples.
  ⚠️ `--kalshi-side no` is complemented by V2 (`buy NO @ 0.01` → `ask @ 0.99`), so on a heavy
  favourite it is NOT "impossible" — a resting YES bid ≥0.99 would cross it. Prefer a mid-priced
  market (both sides ~0.2–0.8) where 1¢ can never cross on either side.

SAFETY GATES (this places REAL orders)
──────────────────────────────────────
  - Requires DRY_RUN=false in .env (in DRY, create_order short-circuits with no network → nothing to
    time). Run deliberately, like the go-live check — NOT something the bot does on its own.
  - Requires --confirm. Without it, prints a preview and exits.
  - Uses impossible prices + asserts zero fill + aborts-and-flattens on any unexpected fill.

Run (deliberate, live creds, zero-fill by design):
  DRY_RUN=false .venv/bin/python -m scripts.order_rtt_probe \
      --kalshi-ticker KXWNBAGAME-...-ATL --poly-token aec-wnba-atl-sea-2026-06-27 --confirm
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from bot.core import config
from bot.kalshi.client import KalshiClient, kalshi_filled_qty
from bot.poly_us.client import PolyUSClient, order_filled_qty, order_is_filled

_IMPOSSIBLE_PRICE = 0.01   # 1¢ — below any tradeable team's ask → FOK can never fill → kills
_PROBE_LABEL = "[RTT-PROBE]"


def _report(name: str, ms: list[float]) -> None:
    if not ms:
        print(f"  {name}: no successful samples")
        return
    print(f"  {name}: median {statistics.median(ms):.0f}ms  min {min(ms):.0f}  "
          f"max {max(ms):.0f}  (n={len(ms)})")


# V2 reports a killed FOK as a 409 ERROR, not a zero-fill response (V1 returned status='canceled').
# That 409 still means the order reached the MATCHING ENGINE and was killed there — i.e. it IS the
# place→engine→response round-trip we want to time. Treat it as a successful sample, not a failure.
_FOK_KILLED_409 = "fill_or_kill_insufficient_resting_volume"


async def _probe_kalshi(ticker: str, side: str, n: int, size: int, price: str) -> list[float]:
    """Time N FOK buys at an impossible price on Kalshi. Aborts on any fill (defense in depth).

    V2 note (migrated 2026-07-14): a killed FOK raises 409 `fill_or_kill_insufficient_resting_volume`
    rather than returning a zero-fill body, and the create response is FLAT (no `order`, no `status`).
    A 409-kill is the GOOD path here — it proves the order traversed the engine. Any OTHER error is a
    real failure and stops this venue.
    """
    client = KalshiClient()
    ms: list[float] = []
    try:
        for i in range(n + 1):  # +1 warmup (dropped)
            t0 = time.perf_counter()
            resp: dict | None = None
            status = "?"
            try:
                resp = await client.create_order(ticker, side, "buy", size, price,
                                                 time_in_force="fill_or_kill")
                dt = (time.perf_counter() - t0) * 1000
                status = "returned"  # V2 create returned a body → engine accepted/filled it
            except Exception as e:
                dt = (time.perf_counter() - t0) * 1000
                if _FOK_KILLED_409 in str(e):
                    status = "killed(409)"       # reached the engine, killed → a VALID RTT sample
                else:
                    print(f"  ❌ order placement failed: {e!r}")
                    break
            filled = kalshi_filled_qty(resp) if resp else 0.0
            if filled > 0:  # impossible at 1¢ — but never trust; flatten + abort
                print(f"  🚨 UNEXPECTED Kalshi fill {filled} — flattening and ABORTING")
                await client.create_order(ticker, side, "sell", int(filled), price,
                                          time_in_force="immediate_or_cancel")
                raise SystemExit("aborted on unexpected fill")
            if i == 0:
                print(f"  (warmup dropped) status={status} fill={filled:.0f}")
                continue
            print(f"  sample {i}: {dt:.0f}ms  status={status} fill={filled:.0f}")
            ms.append(dt)
    finally:
        await client.close()
    return ms


async def _probe_poly(token: str, n: int, size: int, price: float) -> list[float]:
    """Time N FOK buys at an impossible price on Poly US. Aborts on any fill (defense in depth)."""
    client = PolyUSClient()
    ms: list[float] = []
    try:
        for i in range(n + 1):  # +1 warmup (dropped)
            t0 = time.perf_counter()
            try:
                resp = await client.place_limit_fok(token, price, float(size), _PROBE_LABEL)
            except Exception as e:  # endpoint/network error — report, stop THIS venue
                print(f"  ❌ order placement failed: {e!r}")
                break
            dt = (time.perf_counter() - t0) * 1000
            filled = order_filled_qty(resp) if resp else 0.0
            if order_is_filled(resp) or filled > 0:  # impossible at 1¢ — flatten + abort
                print(f"  🚨 UNEXPECTED Poly fill {filled} — flattening and ABORTING")
                try:
                    # (vwap_price, sold_qty) — the sell is IOC and can partial-fill, so say
                    # explicitly whether we actually got flat rather than assuming we did.
                    px, sold = await client.sell_back(token, float(filled or size), _PROBE_LABEL)
                    want = float(filled or size)
                    if sold >= want:
                        print(f"  flattened {sold:.0f} @ {px}")
                    else:
                        print(f"  🚨 FLATTEN INCOMPLETE — sold {sold:.0f}/{want:.0f}; "
                              f"{want - sold:.0f} of {token} STILL HELD. Close manually.")
                finally:
                    raise SystemExit("aborted on unexpected fill")
            status = (resp or {}).get("status", "?")
            if i == 0:
                print(f"  (warmup dropped) status={status} fill={filled:.0f}")
                continue
            print(f"  sample {i}: {dt:.0f}ms  status={status} fill={filled:.0f}")
            ms.append(dt)
    finally:
        await client.close()
    return ms


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kalshi-ticker", required=True)
    ap.add_argument("--kalshi-side", default="yes", choices=["yes", "no"])
    ap.add_argument("--poly-token", required=True)
    ap.add_argument("--n", type=int, default=6, help="timed samples per venue (a warmup is dropped)")
    ap.add_argument("--size", type=int, default=1, help="contracts/shares (kept tiny; never fills)")
    ap.add_argument("--confirm", action="store_true", help="actually place the zero-fill FOK orders")
    args = ap.parse_args()

    price_str = f"{_IMPOSSIBLE_PRICE:.4f}"
    print(f"ORDER-RTT probe — {args.n} FOK buys @ {price_str} (impossible → kills, zero fill), "
          f"size={args.size}")

    if config.DRY_RUN:
        print("\nDRY_RUN=true → create_order short-circuits with NO network call; nothing to time.\n"
              "Re-run with DRY_RUN=false in .env to measure the real order path.")
        return
    if not args.confirm:
        print("\nPREVIEW ONLY (no --confirm). Would place zero-fill FOK buys at 1¢ on:\n"
              f"  Kalshi {args.kalshi_ticker} [{args.kalshi_side}]\n  Poly   {args.poly_token}\n"
              "Re-run with --confirm to place them (real orders, zero fill by design).")
        return

    # Poly first — it's the §5 calibration target (poly_first fires Poly, then Kalshi) and is
    # independent of the Kalshi V1 order-endpoint deprecation (the 410 go-live blocker in TODO). Each
    # venue is isolated: a placement error (e.g. the Kalshi 410) reports and continues to the other;
    # an unexpected FILL still hard-aborts (SystemExit propagates past these `except Exception` guards).
    print(f"\n=== POLY {args.poly_token} ===")
    pms: list[float] = []
    try:
        pms = await _probe_poly(args.poly_token, args.n, args.size, _IMPOSSIBLE_PRICE)
    except Exception as e:
        print(f"  Poly probe errored: {e!r}")

    print(f"\n=== KALSHI {args.kalshi_ticker} [{args.kalshi_side}] ===")
    kms: list[float] = []
    try:
        kms = await _probe_kalshi(args.kalshi_ticker, args.kalshi_side, args.n, args.size, price_str)
    except Exception as e:
        print(f"  Kalshi probe errored: {e!r}")

    print("\n──────── ORDER place→kill RTT (incl. matching engine) ────────")
    _report("Poly   ORDER RTT", pms)
    _report("Kalshi ORDER RTT", kms)
    print("\nPoly ORDER RTT is the calibration target for KALSHI_INTERLEG_PROBE_DELAY_MS "
          "(poly_first fires Poly, then Kalshi). A kill goes through the same engine path as a real "
          "FOK fire, so this is the closest safe proxy for the place→fill round-trip; a true FILLING "
          "order may differ slightly. Compare to latency_probe.py reads + the TCP network leg.")


if __name__ == "__main__":
    asyncio.run(main())
