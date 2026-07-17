"""
scripts/poly_us_ws_capture.py
─────────────────────────────
Read-only: connect to the Polymarket US markets WebSocket with our own raw
transport (auth + subscribe from bot/poly_us/feed.py) and print the raw frames.

Validates, against the live server, before flipping POLY_US_FEED_SOURCE=raw:
  - the Ed25519 handshake auth is accepted (connection succeeds)
  - the marketData message shape matches our parser (marketData.offers[].px.value)
  - heartbeat frames arrive ({"heartbeat": {}}) so the liveness watchdog is sound

Run:  .venv/bin/python -m scripts.poly_us_ws_capture <market-slug> [more slugs...]
A slug looks like "atc-fwc-mex-rsa-2026-06-11-mex" (the value carried in
MarketPair.token_yes_a/b). Needs POLYMARKET_US_KEY_ID/SECRET_KEY in .env.
"""
from __future__ import annotations

import asyncio
import json
import sys

import websockets

from bot.poly_us.feed import PolyUSOrderBookCache, _WS_URL


async def main() -> None:
    slugs = sys.argv[1:]
    if not slugs:
        print("usage: python -m scripts.poly_us_ws_capture <market-slug> [more...]")
        return

    cache = PolyUSOrderBookCache(sdk=None)  # reuse its auth + subscribe builders
    print(f"connecting {_WS_URL}  slugs={slugs}")
    async with websockets.connect(
        _WS_URL, additional_headers=cache._auth_headers(),
        ping_interval=20, ping_timeout=20, max_size=None,
    ) as ws:
        sub = cache._subscribe_payload(slugs)
        await ws.send(json.dumps(sub))
        print("subscribed:", json.dumps(sub))
        n = 0
        while n < 30:  # print first 30 frames then stop
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                print("(no frame for 30s — stopping)")
                break
            n += 1
            try:
                msg = json.loads(raw)
                key = next((k for k in ("marketData", "marketDataLite", "trade",
                                        "heartbeat", "error") if k in msg), "?")
                print(f"[{n}] {key}: {json.dumps(msg)[:400]}")
            except json.JSONDecodeError:
                print(f"[{n}] non-JSON: {raw[:200]!r}")


if __name__ == "__main__":
    asyncio.run(main())
