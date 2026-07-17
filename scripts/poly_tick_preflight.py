"""
scripts/poly_tick_preflight.py — go-live preflight: Poly off-tick order behavior
────────────────────────────────────────────────────────────────────────────────
Answers the one-time go-live question "does Poly REJECT an off-tick limit price, or
SNAP it?" — the blocker that decides whether the fire-path `poly_limit = round(ask+buf, 4)`
(bot/runner/kalshi_arb.py) is wire-safe. Verdict as of 2026-06-24: Poly **floors** an
off-tick BUY limit to its 0.01 tick (0.8599→0.85, 0.0160→0.01; floor, NOT round-to-nearest)
and does **not** 400-reject. See the PROJECT_STATE "Poly order tick-legality" row.

ZERO CAPITAL. Calls `orders.preview` ONLY (POST /v1/order/preview) — never `orders.create`.
Preview validates an order server-side and returns the resulting Order without placing it:
no order, no capital, no position. Belt-and-suspenders, every probe price is a deliberately
NON-marketable low BUY, so even a hypothetical mis-fire would FOK-kill at zero cost.

It IS a live authenticated call (real account creds), so this is a manual preflight, NOT a
unit test — it must never live in tests/ (the suite is pure+mocked and runs on every Stop hook).
Re-run it to re-confirm the venue's tick behavior hasn't changed before a go-live decision.

Run:  .venv/bin/python -m scripts.poly_tick_preflight
"""
from __future__ import annotations

import asyncio

from polymarket_us import AsyncPolymarketUS

from bot.core import config
from bot.poly_us.scanner import PolyUSScanner

# Non-marketable low BUY prices. Boundary sweep (~0.01 tick) pins floor-vs-round; the 0.85xx
# row re-checks the rule in a realistic price range and includes the historical blocker example.
_PROBE_PRICES = (0.0140, 0.0149, 0.0150, 0.0151, 0.0160, 0.8540, 0.8550, 0.8560, 0.8599)


async def _preview_snap(sdk: AsyncPolymarketUS, slug: str, price: float) -> str:
    """Preview a non-marketable BUY_LONG at `price`; return 'price -> echoed' or a reject."""
    params = {
        "request": {
            "marketSlug": slug,
            "intent": "ORDER_INTENT_BUY_LONG",
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.4f}", "currency": "USD"},
            "quantity": 1,
            "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
    }
    try:
        resp = await sdk.orders.preview(params)
        order = resp.get("order", {}) if isinstance(resp, dict) else {}
        echoed = (order.get("price") or {}).get("value")
        return f"{price:.4f} -> {echoed}"
    except Exception as exc:  # an off-tick REJECT would surface here
        return f"{price:.4f} -> REJECT/{type(exc).__name__}: {exc}"


async def main() -> None:
    sdk = AsyncPolymarketUS(
        key_id=config.POLYMARKET_US_KEY_ID or None,
        secret_key=config.POLYMARKET_US_SECRET_KEY or None,
    )
    try:
        # A live, orderable slug via the SAME scanner the bot uses. token_yes_a is the bare
        # long-side slug (a bare slug = BUY_LONG per bot.poly_us.sides.parse_token).
        pairs = await PolyUSScanner(sdk).fetch_markets(config.POLYMARKET_US_SERIES)
        slug = next((p.token_yes_a for p in pairs if p.token_yes_a), None)
        if not slug:
            print(f"No orderable slug from {len(pairs)} scanned pairs — no live markets right "
                  "now? Re-run during a live game window.")
            return
        print(f"slug = {slug}   ({len(pairs)} pairs scanned)\n")

        results = [await _preview_snap(sdk, slug, px) for px in _PROBE_PRICES]
        for line in results:
            print("  " + line)

        rejected = any("REJECT" in r for r in results)
        print()
        if rejected:
            print("VERDICT: Poly REJECTS off-tick — poly_limit MUST be tick-snapped before go-live.")
        else:
            print("VERDICT: Poly ACCEPTS + snaps off-tick (no reject) — round(…,4) is wire-safe.")
            print("         Moot for the live buy leg since S4 (2026-07-16): poly_limit is now the")
            print("         raw ask, flat — a resting price (or 1−bid, and the tick divides 1.0),")
            print("         so it is ON-tick by construction. The dropped buffer was the only")
            print("         thing that could push it off-grid. Still worth running before go-live")
            print("         to confirm the snap DIRECTION if a limit ever goes off-tick again.")
    finally:
        await sdk.close()


if __name__ == "__main__":
    asyncio.run(main())
