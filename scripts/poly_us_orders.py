"""
scripts/poly_us_orders.py
─────────────────────────
Read-only dump of your Polymarket US order activity, positions, and open orders.
The Poly US app doesn't surface order history, but the API does. Use this to see
what actually filled (and the raw JSON shapes) — safe, makes no trades.

Run:  .venv/bin/python -m scripts.poly_us_orders
"""
from __future__ import annotations

import asyncio
import json

from polymarket_us import AsyncPolymarketUS

from bot.core import config


async def main() -> None:
    if not config.POLYMARKET_US_KEY_ID or not config.POLYMARKET_US_SECRET_KEY:
        raise SystemExit("Set POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY in .env")

    sdk = AsyncPolymarketUS(
        key_id=config.POLYMARKET_US_KEY_ID,
        secret_key=config.POLYMARKET_US_SECRET_KEY,
    )
    try:
        print("=== RECENT TRADES (most recent first) ===")
        acts = await sdk.portfolio.activities(params={
            "types": ["ACTIVITY_TYPE_TRADE"],
            "sortOrder": "SORT_ORDER_DESCENDING",
            "limit": 50,
        })
        activities = acts.get("activities", []) if isinstance(acts, dict) else []
        if not activities:
            print("  (none)")
        for i, a in enumerate(activities):
            t = a.get("trade", {})
            px = (t.get("price") or {}).get("value")
            pnl = (t.get("realizedPnl") or {}).get("value")
            print(f"  {t.get('createTime','?')}  {t.get('marketSlug','?')}  "
                  f"qty={t.get('qty','?')} px={px} state={t.get('state','?')} "
                  f"aggressor={t.get('isAggressor')} pnl={pnl}")
        if activities:
            print("\n  --- raw shape of most recent trade ---")
            print("  " + json.dumps(activities[0], indent=2).replace("\n", "\n  "))

        print("\n=== CURRENT POSITIONS ===")
        pos = await sdk.portfolio.positions()
        positions = pos.get("positions", {}) if isinstance(pos, dict) else {}
        if not positions:
            print("  (flat)")
        for slug, p in positions.items():
            print(f"  {slug}  net={p.get('netPosition')} "
                  f"avail={p.get('qtyAvailable')} cost={(p.get('cost') or {}).get('value')}")

        print("\n=== OPEN ORDERS ===")
        orders = await sdk.orders.list()
        open_orders = orders.get("orders", []) if isinstance(orders, dict) else orders
        print(f"  {open_orders}")
    finally:
        await sdk.close()


if __name__ == "__main__":
    asyncio.run(main())
