"""Poly taker fee — pinned to REAL FILLS, not to the code's own arithmetic.

The Kalshi fee model is pinned to three real fills (tests/test_kalshi_fee_model.py); the Poly twin
was pinned to `assert _effective_share_cost(p, 0.05) == 0.85 + 0.05*0.85*0.15` — the code asserted
against itself, which can only ever confirm the belief it was written from. These are real
commissions the exchange actually charged, read back from /v1/order/{id} + portfolio/activities.
"""
import pytest

from bot.kalshi.cross_arb import _effective_share_cost
from bot.poly_us.scanner import _POLY_US_TAKER_THETA

# (label, p, C, actual commissionNotionalTotalCollected, theta in force at the time)
# [VERIFIED 2026-07-15 — read back from the venue's own order records]
REAL_FILLS = [
    ("MLS futures  2026-07-15", 0.008, 255, 0.12, 0.06),
    ("MLS futures  2026-07-15", 0.009, 255, 0.14, 0.06),
    ("WC moneyline 2026-06-16", 0.650,  46, 0.52, 0.05),
    ("WC moneyline 2026-06-16", 0.610,  16, 0.19, 0.05),
    ("WC moneyline 2026-06-17", 0.620,  15, 0.18, 0.05),
    ("WC moneyline 2026-06-17", 0.310,  15, 0.16, 0.05),
    ("WC moneyline 2026-06-17", 0.260,  14, 0.13, 0.05),
    ("WC moneyline 2026-06-17", 0.270,  14, 0.14, 0.05),
    ("WC moneyline 2026-06-16", 0.670,  14, 0.15, 0.05),
]


@pytest.mark.parametrize("label,p,C,actual,theta", REAL_FILLS, ids=[f"{f[0]}@{f[1]}" for f in REAL_FILLS])
def test_fee_formula_matches_real_exchange_commissions(label, p, C, actual, theta):
    """fee = round(Θ·p·(1−p)·C, 2) — the parabola AND the round-to-cent on the ORDER TOTAL.
    All 9 real fills match exactly at the Θ in force when they traded."""
    assert round(theta * p * (1 - p) * C + 1e-12, 2) == pytest.approx(actual), \
        f"{label}: model disagrees with what the exchange actually charged"


def test_poly_theta_fallback_matches_the_venues_current_coefficient():
    """Poly RAISED its coefficient 0.05 → 0.06 between 2026-06-17 and 2026-07-15 — proven by the
    fills above (June fit 0.05 exactly, July fit 0.06 exactly) and by the venue now reporting
    feeCoefficient=0.06 on every live market.

    The live path self-corrected because the scanner reads feeCoefficient per-market. This pins the
    FALLBACK, which does not: a silent rename of that field drops us to the hardcoded value with no
    error, and a stale 0.05 UNDERSTATES the fee → OVERSTATES every edge by ~0.25c/share at p=0.5,
    ~12% of the 2% floor. Fails the dangerous way (fires trades that aren't really above the floor).
    """
    assert _POLY_US_TAKER_THETA == 0.06


def test_effective_share_cost_charges_the_fee_on_every_sport():
    """The docstring claimed 'NBA/NHL (fee_rate=0): effective_cost = p_poly'. FALSE — all 29 live
    pairs, including all 17 NBA, report feeCoefficient=0.06. Nothing is fee-free."""
    assert _effective_share_cost(0.50, 0.06) == pytest.approx(0.515)
    assert _effective_share_cost(0.50, 0.06) > 0.50
