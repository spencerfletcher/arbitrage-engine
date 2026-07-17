"""Position size is MAX_POSITION_USD alone (S1).

CROSS_ARB_SIZE_TIERS never did anything on the live path: its breakpoints (0.005/0.01) are
sportsbook-era and sit BELOW the live fire floor KALSHI_ARB_MIN_EDGE=0.02, so every edge that can
fire landed in the top tier and _edge_size_multiplier returned a constant 1.0. Deleted rather than
re-tuned — a knob that provably does nothing, while CLAUDE.md + position_management.md + README all
credited it for the 5-10 share ramp, is worse than no knob: it invites "raise MAX_POSITION_USD, the
tiers will damp it" (they won't — 5->500 is an unmodulated 100x step).

The docs also recommended `0.02:1.5` = 1.5x size on FAT edges, which config.py explicitly forbids:
cross-venue a fat edge means the venues DISAGREE (one is stale) = peak strand risk. That intuition
was right for same-platform arb, where both prices came from one venue.
"""
import pytest

from bot.core import config


def test_the_tier_knob_is_gone():
    """No CROSS_ARB_SIZE_TIERS, no _edge_size_multiplier — nothing left to mis-credit."""
    assert not hasattr(config, "CROSS_ARB_SIZE_TIERS")
    import bot.kalshi.cross_arb as ca
    assert not hasattr(ca, "_edge_size_multiplier")


@pytest.mark.parametrize("edge", [0.02, 0.03, 0.05, 0.10, 0.33, 0.49])
def test_size_is_the_budget_over_the_dearer_leg_at_every_fireable_edge(edge, monkeypatch):
    """Size depends on PRICE, never on edge. Identical at a 2% and a 49% edge — that is the
    'do NOT size UP on fat edges' rule the tiers pretended to implement."""
    import math
    monkeypatch.setattr(config, "MAX_POSITION_USD", 5.0)
    poly_eff, kalshi_eff = 0.5125, 0.45
    expected = max(1, math.floor(config.MAX_POSITION_USD / max(poly_eff, kalshi_eff)))
    assert expected == 9
