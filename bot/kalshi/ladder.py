"""
bot/kalshi/ladder.py
────────────────────
Kalshi market-shape classification.

⚠️ The single-venue crypto-LADDER ARB was DELETED 2026-07-15 — this module is what survives it.
The ladder bought "≥Xi" YES + "≥Xj" NO within one Kalshi event; it never found an arb, ran live with
no on/off flag, had no test coverage of its executor, and held two silent naked-position bugs. It
also neutered the frozen-book watchdog: subscribing KXBTCD/KXBTCMAXY put 24/7 BTC ticks on the
socket, and the watchdog reads a single socket-wide last-change timestamp. See docs/TODO.md S5.

`classify_market` outlives it because macro_pairs (Fed/CPI) uses the same threshold-vs-bucket market
shape — that is a Kalshi structural fact, not a ladder concept.
"""
from __future__ import annotations


def classify_market(market: dict) -> str:
    """Classify a Kalshi market dict: 'threshold', 'bucket', or 'skip'.

    threshold: cumulative "≥X" (floor_strike set, cap_strike None).
    bucket:    exclusive range "$X to $Y" (both strikes set).
    skip:      neither strike present.
    """
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    if floor is None:
        return "skip"
    return "bucket" if cap is not None else "threshold"
