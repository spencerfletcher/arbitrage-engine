"""
scripts/kalshi_orderbook_capture.py
───────────────────────────────────
Read-only: subscribe to Kalshi's orderbook_delta channel for a few tickers and
print the raw messages. Confirms the exact subscribe param name, message keys,
and seq behavior before we build book maintenance on top of them.

Run:  .venv/bin/python -m scripts.kalshi_orderbook_capture KXWCGAME-26JUN17ENGCRO-ENG [more tickers...]
(no args → captures whatever the channel sends for a couple sample tickers)
"""
from __future__ import annotations

import asyncio
import json
import sys

import websockets

from bot.kalshi.client import KalshiClient


async def main() -> None:
    tickers = sys.argv[1:]
    client = KalshiClient()
    url = client.ws_url
    print(f"connecting {url}  tickers={tickers or '(none — server default)'}")
    async with websockets.connect(
        url, additional_headers=client.ws_headers(),
        ping_interval=20, ping_timeout=20,
    ) as ws:
        sub = {"id": 1, "cmd": "subscribe",
               "params": {"channels": ["orderbook_delta"]}}
        if tickers:
            sub["params"]["market_tickers"] = tickers
        await ws.send(json.dumps(sub))
        print("subscribed:", json.dumps(sub))
        n = 0
        while n < 30:  # print first 30 messages then stop
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                print("(no message in 30s)")
                continue
            print(raw)
            n += 1


if __name__ == "__main__":
    asyncio.run(main())
