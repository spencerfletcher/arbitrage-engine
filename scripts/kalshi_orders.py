"""
scripts/kalshi_orders.py
────────────────────────
Read-only dump of recent Kalshi orders and fills. Use this to see whether the
bot's Kalshi legs were accepted-then-killed (status "canceled") vs never created.

NOTE: orders rejected at validation (e.g. invalid price tick → HTTP 400) are
never created, so they will NOT appear here — those show only as the error body
now logged by KalshiClient._post. This script catches accepted-but-unfilled
(canceled) FOKs and actual fills.

Run:  .venv/bin/python -m scripts.kalshi_orders
"""
from __future__ import annotations

import asyncio
import json

from bot.kalshi.client import KalshiClient


async def main() -> None:
    client = KalshiClient()
    print("=== RECENT ORDERS (most recent first) ===")
    orders_resp = await client._get("/portfolio/orders", params={"limit": 50})
    orders = orders_resp.get("orders", []) if isinstance(orders_resp, dict) else []
    if not orders:
        print("  (none)")
    for o in orders:
        print(f"  {o.get('created_time','?')}  {o.get('ticker','?')}  "
              f"{o.get('action','?')}/{o.get('side','?')}  "
              f"status={o.get('status','?')}  count={o.get('count','?')}  "
              f"yes_price={o.get('yes_price','?')}  filled={o.get('fill_count','?')}")
    if orders:
        print("\n  --- raw shape of most recent order ---")
        print("  " + json.dumps(orders[0], indent=2).replace("\n", "\n  "))

    print("\n=== RECENT FILLS ===")
    fills_resp = await client._get("/portfolio/fills", params={"limit": 50})
    fills = fills_resp.get("fills", []) if isinstance(fills_resp, dict) else []
    if not fills:
        print("  (none)")
    for f in fills:
        print(f"  {f.get('created_time','?')}  {f.get('ticker','?')}  "
              f"{f.get('action','?')}/{f.get('side','?')}  "
              f"count={f.get('count','?')}  yes_price={f.get('yes_price','?')}")


if __name__ == "__main__":
    asyncio.run(main())
