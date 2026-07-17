"""
scripts/poly_fok_probe.py
─────────────────────────
Settle ONE question: **does Polymarket US honor `tif=TIME_IN_FORCE_FILL_OR_KILL`?**

It matters because the Poly leg's fill semantics decide whether a partial fill is possible, and
therefore whether every cross-arb caller MUST reconcile to the actual fill (it must — see
`order_filled_qty`). The docs say FOK is supported ("must fill entirely or cancel",
docs.polymarket.us/api-reference/orders/create-order), so the code asked for it for a long time
on the assumption it was honored.

WHAT THE FREE EVIDENCE ALREADY SAYS (2026-07-15, /v1/order/preview, zero capital)
────────────────────────────────────────────────────────────────────────────────
`POST /v1/order/preview` takes a full CreateOrderParams (incl. `tif`) and places nothing. Sending
FOK there echoes back `TIME_IN_FORCE_IMMEDIATE_OR_CANCEL`. Controls show preview is FAITHFUL, not
normalizing:
    TIME_IN_FORCE_DAY            -> DAY            preserved  (docs-only; not even in the SDK enum)
    TIME_IN_FORCE_GOOD_TILL_DATE -> GOOD_TILL_DATE preserved
    TIME_IN_FORCE_GOOD_TILL_CANCEL / IMMEDIATE_OR_CANCEL      preserved
    TOTAL_GARBAGE_VALUE          -> DAY            silently DEFAULTED (unknown -> proto enum default)
    TIME_IN_FORCE_FILL_OR_KILL   -> IMMEDIATE_OR_CANCEL   *** specifically downgraded ***
An unsupported value defaults to DAY. FOK does NOT — it maps specifically to IOC, i.e. the server
recognises FOK and deliberately implements it as IOC. No parameter combo avoids it (sync on/off/
absent, manualOrderIndicator absent, participateDontInitiate, qty=1 that FITS in the book).

WHY THIS SCRIPT STILL EXISTS
────────────────────────────
All of the above is `preview`. It does NOT prove `create` applies the same rule — and only `create`
moves money. This places ONE real order to settle it, because the answer changes what callers must
defend against.

HOW IT STAYS CHEAP (worst case ~$2-3, and it is bounded, not hoped-for)
──────────────────────────────────────────────────────────────────────
Buy MORE than the book offers at our limit, on a deliberately cheap, thin, long-shot futures market
(default: an MLS champion long-shot at <=1c, outside our trading universe so it perturbs nothing):
  - FOK honored  -> fills 0, KILLS. Costs $0. Question answered.
  - FOK downgraded -> IOC partial-fills the available depth. We bought something -> UNWIND NOW.
`--max-cost` HARD-caps the worst case (default $5): the probe refuses to place if
depth_at_limit x price exceeds it. The fill is bounded by book depth at our limit, which we read
immediately beforehand.

A fill is NOT a failure — it is the answer, and its unwind also yields a real measured Poly flatten
cost (an input the exec-order decision wants anyway). The script unwinds via the same `sell_back`
the bot's real unwind path uses, and reports the realized round-trip cost.

Run (deliberate, live creds, places ONE real order):
  DRY_RUN=false .venv/bin/python -m scripts.poly_fok_probe --confirm
"""
from __future__ import annotations

import argparse
import asyncio
import json

from bot.core import config
from bot.poly_us.client import PolyUSClient, order_filled_qty

_FOK = "TIME_IN_FORCE_FILL_OR_KILL"
_DEFAULT_SLUG = "tec-mls-winner-2026-11-07-dcu"   # <=1c MLS long-shot: cheap, thin, not ours
_LABEL = "[FOK-PROBE]"


def _order_params(slug: str, price: float, qty: int, tif: str) -> dict:
    """EXACTLY what bot/poly_us/client.py:place_limit_fok sends — same fields, same tif.
    The point is to test the REAL call, not a lookalike."""
    return {
        "marketSlug": slug,
        "intent": "ORDER_INTENT_BUY_LONG",
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": f"{price:.4f}", "currency": "USD"},
        "quantity": qty,
        "tif": tif,
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        "synchronousExecution": True,
    }


def _echoed_tif(resp: dict) -> str | None:
    """The tif the EXCHANGE says the order has (create echoes the Order under executions[].order).
    This is the authoritative answer — not what we asked for."""
    for ex in (resp or {}).get("executions", []) or []:
        order = ex.get("order", {}) if isinstance(ex, dict) else {}
        if order.get("tif"):
            return order["tif"]
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default=_DEFAULT_SLUG)
    ap.add_argument("--max-cost", type=float, default=5.0,
                    help="HARD cap: refuse to place if the worst-case fill exceeds this ($)")
    ap.add_argument("--confirm", action="store_true", help="actually place the real order")
    args = ap.parse_args()

    client = PolyUSClient()
    try:
        ask, state, levels, _tt, _stats = await client.get_fill_quote(args.slug, fresh=True)
        if not levels or ask is None:
            print(f"No book for {args.slug} (state={state}) — pick another --slug."); return
        limit = float(ask)
        depth_at_limit = sum(q for p, q in levels if p <= limit)
        qty = int(depth_at_limit) + 45          # deliberately MORE than is available at `limit`
        worst_case = depth_at_limit * limit     # an IOC can fill at most the depth at our limit

        print(f"market   {args.slug}  state={state}")
        print(f"book     ask={limit}  depth@<={limit}={depth_at_limit:.0f}  levels={levels[:3]}")
        print(f"order    BUY {qty} @ {limit:.4f}  tif={_FOK}   (qty > depth → FOK must KILL if honored)")
        print(f"worst    IOC fills {depth_at_limit:.0f} @ {limit:.4f} = ${worst_case:.2f} "
              f"(cap ${args.max_cost:.2f})")

        if worst_case > args.max_cost:
            print(f"\nREFUSED: worst case ${worst_case:.2f} > --max-cost ${args.max_cost:.2f}.")
            return
        if config.DRY_RUN:
            print("\nDRY_RUN=true → no real order. Re-run with DRY_RUN=false to settle create.")
            return
        if not args.confirm:
            print("\nPREVIEW ONLY (no --confirm). Re-run with --confirm to place the real order.")
            return

        resp = await client._sdk.orders.create(_order_params(args.slug, limit, qty, _FOK))
        filled = order_filled_qty(resp)
        tif = _echoed_tif(resp)

        print(f"\n─── RESULT ───")
        print(f"  tif SENT      {_FOK}")
        print(f"  tif ECHOED    {tif}")
        print(f"  requested     {qty}")
        print(f"  cumQuantity   {filled:.0f}")
        if filled <= 0 and tif == _FOK:
            print("\n  ✅ FOK IS HONORED — order killed, zero fill. The Poly leg IS all-or-none.")
        elif filled <= 0:
            print(f"\n  ⚠️ AMBIGUOUS — zero fill but tif echoed {tif}. It may have killed as IOC with"
                  f"\n     no liquidity, or honored FOK. Re-run when the book is deeper.")
        else:
            print(f"\n  ❌ FOK IS NOT HONORED — tif came back {tif} and it PARTIAL-FILLED "
                  f"{filled:.0f}/{qty}."
                  f"\n     The Poly leg is IOC and CAN partial-fill. Every caller MUST size the"
                  f"\n     opposite leg off the ACTUAL fill (order_filled_qty), never assume all-or-none.")
            print(f"\n  Unwinding {filled:.0f} now via the bot's real sell_back path…")
            # (vwap_price, sold_qty) — the sell is IOC too, so it can itself partial-fill.
            px, sold = await client.sell_back(args.slug, float(filled), _LABEL)
            if sold <= 0:
                print(f"  🚨 UNWIND FAILED — {filled:.0f} shares of {args.slug} still HELD. "
                      f"Close manually.")
            else:
                cost = (limit - float(px)) * sold
                print(f"  unwound {sold:.0f}/{filled:.0f} at {px} → realized round-trip cost "
                      f"${cost:.4f} (${cost/sold:.5f}/share) — a REAL measured Poly flatten cost.")
                if sold < filled:
                    print(f"  🚨 {filled - sold:.0f} shares of {args.slug} STILL HELD — "
                          f"close manually.")
        print(f"\n  raw: {json.dumps(resp)[:400]}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
