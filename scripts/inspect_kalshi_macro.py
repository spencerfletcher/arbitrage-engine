"""Inspect KXFED/KXCPI markets: show threshold and binary markets with prices.

Flags markets closing within DAYS days so you can prioritise near-term pairs.
"""
import asyncio
import datetime

from bot.kalshi.client import KalshiClient
from bot.kalshi.ladder import classify_market
from bot.core.matcher import parse_iso

DAYS = 30  # flag markets closing within this many days


async def main() -> None:
    c = KalshiClient()
    cutoff = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=DAYS)

    for series in ["KXFED", "KXCPI"]:
        events = await c.get_events(series)
        print(f"\n{'='*60}")
        print(f"  {series}: {len(events)} events")
        print(f"{'='*60}")
        for ev in events:
            markets = ev.get("markets", [])
            for m in markets:
                kind = classify_market(m)
                if kind == "bucket":
                    continue  # range buckets: not usable cross-platform

                close_raw = m.get("expected_expiration_time") or m.get("close_time") or ""
                close_dt = parse_iso(close_raw)
                near = close_dt is not None and close_dt <= cutoff
                tag = " *** NEAR TERM ***" if near else ""

                yes_ask = m.get("yes_ask_dollars") or "?"
                no_ask  = m.get("no_ask_dollars") or "?"
                try:
                    combined = round(float(yes_ask) + float(no_ask), 4)
                    spread_tag = f"  combined={combined:.4f}"
                except (TypeError, ValueError):
                    spread_tag = ""

                print(f"\n  {m['ticker']}  [{kind}]{tag}")
                print(f"    close : {close_raw}")
                print(f"    YES   : '{m.get('yes_sub_title')}' ask={yes_ask}")
                print(f"    NO    : '{m.get('no_sub_title')}' ask={no_ask}{spread_tag}")
                if m.get("floor_strike") is not None:
                    print(f"    floor : {m.get('floor_strike')}")


asyncio.run(main())
