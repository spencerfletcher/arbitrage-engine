"""
scripts/poly_us_reward_probe.py
───────────────────────────────
READ-ONLY market-making recon for the live Poly US slate. Two questions:

  1. Are liquidity-reward / maker-incentive params (max_incentive_spread,
     min_incentive_size, daily pool) exposed on the trading API's market objects?
     FINDING 2026-06-28: NO — events.list, markets.list, markets.retrieve_by_slug
     carry none, and the SDK has no rewards namespace. Reward pool sizes must come
     from Polymarket's rewards dashboard/docs, not this API.
  2. Is there spread to capture / flow to capture it from? Summarizes live winner-
     market spreads, at-tick share, and 24h volume — the MM-viability microstructure.

Makes NO trades. No auth needed for public reads. Run:
  .venv/bin/python -m scripts.poly_us_reward_probe
"""
from __future__ import annotations

import asyncio
import statistics
from datetime import datetime, timezone

from bot.core import config

# substrings that flag a reward/incentive field at any nesting depth (question 1)
_HINTS = ("reward", "incentive", "maxspread", "minsize", "min_size", "rebate", "epoch")
_SERIES_NAMES = {"69": "WorldCup", "15": "MLB", "4": "NBA", "6": "NHL", "49": "WNBA"}
_WINDOW_H = (-3.0, 48.0)  # game-start window counted as "tradeable now"


def _amount(x) -> float | None:
    """Poly US quotes are {'value': '0.5450', 'currency': 'USD'} — unwrap to float."""
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _hours_to_start(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (t - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except ValueError:
        return None


def _reward_fields(obj, pre="") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower().replace("_", "")
            p = f"{pre}.{k}" if pre else str(k)
            if any(h.replace("_", "") in kl for h in _HINTS):
                out.append((p, v if not isinstance(v, (dict, list)) else f"<{type(v).__name__}>"))
            out += _reward_fields(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            out += _reward_fields(v, f"{pre}[{i}]")
    return out


def _is_winner(smt: str) -> bool:
    return smt.endswith("_team_full_game_winner") or smt in (
        "moneyline", "drawable_outcome", "soccer_team_full_time_winner")


async def main() -> None:
    from polymarket_us import AsyncPolymarketUS

    sdk = AsyncPolymarketUS(
        key_id=config.POLYMARKET_US_KEY_ID or None,
        secret_key=config.POLYMARKET_US_SECRET_KEY or None,
    )
    reward_fields_seen = False
    try:
        for sid in config.POLYMARKET_US_SERIES:
            label = _SERIES_NAMES.get(sid, sid)
            try:
                resp = await sdk.events.list(params={
                    "seriesId": sid, "active": True, "closed": False, "limit": 100, "offset": 0})
            except Exception as exc:
                print(f"\n=== {label} (sid={sid}) — FETCH ERROR: {exc!r}")
                continue
            events = resp.get("events", []) if isinstance(resp, dict) else []

            rows: list[tuple[float, float, float | None, float | None]] = []  # spread, vol24, tick, hrs
            for ev in events:
                for m in ev.get("markets", []):
                    if reward_fields_seen is False and _reward_fields(m):
                        reward_fields_seen = True
                    if not _is_winner(m.get("sportsMarketType", "")):
                        continue
                    ask, bid = _amount(m.get("bestAskQuote")), _amount(m.get("bestBidQuote"))
                    if ask is None or bid is None:
                        continue
                    rows.append((ask - bid, _amount(m.get("volume24hr")) or 0.0,
                                 _amount(m.get("orderPriceMinTickSize")),
                                 _hours_to_start(m.get("gameStartTime"))))

            window = [r for r in rows if r[3] is not None and _WINDOW_H[0] <= r[3] <= _WINDOW_H[1]]
            sample = window or rows
            if not sample:
                print(f"\n=== {label} (sid={sid}) — {len(events)} events, no quoted winner markets")
                continue
            spreads = sorted(r[0] for r in sample)
            vols = [r[1] for r in sample]
            ticks = sorted({r[2] for r in sample if r[2]})
            at_tick = sum(1 for r in sample if r[2] and r[0] <= r[2] + 1e-9)
            print(f"\n=== {label} (sid={sid}) — {len(rows)} quoted winner-mkts, "
                  f"{len(window)} in {_WINDOW_H[0]:g}h..{_WINDOW_H[1]:g}h window ===")
            print(f"  spread $: min/med/max = {spreads[0]:.4f}/{statistics.median(spreads):.4f}/{spreads[-1]:.4f}")
            print(f"  AT minimum tick (≈no spread to capture): {at_tick}/{len(sample)}")
            print(f"  vol24hr: med={statistics.median(vols):,.0f} max={max(vols):,.0f} | tick(s)={ticks}")

        print(f"\nReward/incentive fields on market objects: "
              f"{'FOUND (investigate)' if reward_fields_seen else 'NONE — pools are off-API (dashboard/docs only)'}")
    finally:
        await sdk.close()


if __name__ == "__main__":
    asyncio.run(main())
