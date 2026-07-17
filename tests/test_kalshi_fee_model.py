"""Kalshi taker-fee model — pinned against REAL MEASURED FILLS, not the schedule's display table.

Why this file exists: the fee is the core of the edge (`edge = 1 - poly_effective - kalshi_effective`,
CLAUDE.md: "Edges are ALWAYS net of taker fees"), and it was WRONG from the start — it ceiled to the
CENT because someone implemented the fee schedule's human-readable convenience table ("$0.30 → $0.02
for 1 contract", pp.4-5) instead of the formula printed above it:

    fees = round up(M x 0.07 x C x P x (1-P))
    "round up = rounds up such that the fee + positionCost is rounded to a centicent"
                                                                     ^^^^^^^^^ 1e-4, not 1e-2

That 100x precision error overstated the fee by up to ~0.9c/contract, understating EVERY edge and
rejecting ~10% of `below_min_edge` opportunities (327/3234) that actually cleared the 2% net floor.

The pins below are ACTUAL CHARGES from real fills on 2026-07-14 — ground truth, not doc-reading.
If someone "simplifies" this back to the table, these fail.
"""
import math

import pytest

from bot.kalshi.cross_arb import _kalshi_taker_fee, check_kalshi_arb


# ── Ground truth: what Kalshi ACTUALLY charged (fee_cost from /portfolio/fills) ──────────
# (price, count, actual_total_fee_dollars, venue, note)
REAL_FILLS = [
    (0.4270, 1, 0.017200, "prod", "sub-cent fill price; centicent ceil of 0.017127"),
    (0.45,   3, 0.052000, "demo", "excludes PER-CONTRACT ceiling (that would be 0.0522)"),
    (0.30,   1, 0.014700, "demo", "exact centicent; excludes our old cent-ceil 0.02"),
]


@pytest.mark.parametrize("price,count,actual,venue,note", REAL_FILLS)
def test_matches_real_measured_fills(price, count, actual, venue, note):
    """THE pin: reproduce what the exchange actually charged, to the centicent."""
    assert _kalshi_taker_fee(price, count) * count == pytest.approx(actual, abs=5e-7), note


@pytest.mark.parametrize("price,count,actual,venue,note", REAL_FILLS)
def test_the_old_cent_ceiling_would_have_been_wrong(price, count, actual, venue, note):
    """Regression guard: the OLD model (ceil to cent) must NOT reproduce reality.
    If this ever passes, someone reverted the precision to the display table."""
    old = math.ceil(0.07 * count * price * (1 - price) * 100) / 100
    assert old != pytest.approx(actual, abs=5e-7), "old cent-ceil model must not match reality"


def test_ceiling_is_on_the_total_not_per_contract():
    """3 @ 0.45 discriminates: per-contract ceiling gives 0.0522, total ceiling gives 0.0520.
    Kalshi charged 0.0520."""
    per_contract_ceil = math.ceil(0.07 * 0.45 * 0.55 * 10000) / 10000 * 3
    total_ceil = math.ceil(0.07 * 3 * 0.45 * 0.55 * 10000) / 10000
    assert per_contract_ceil == pytest.approx(0.0522)
    assert total_ceil == pytest.approx(0.0520)
    assert _kalshi_taker_fee(0.45, 3) * 3 == pytest.approx(0.0520, abs=5e-7)


def test_ceils_to_centicent_never_below_the_raw_formula():
    """Fee must never be UNDER the raw formula (Kalshi rounds UP), and never more than
    one centicent over it."""
    for p in [i / 100 for i in range(1, 100)]:
        for n in (1, 3, 10, 137):
            raw = 0.07 * n * p * (1 - p)
            got = _kalshi_taker_fee(p, n) * n
            assert got >= raw - 1e-12, f"fee under-charges at p={p} n={n}"
            assert got - raw <= 1e-4 + 1e-12, f"fee more than a centicent over at p={p} n={n}"


def test_rate_is_007_recovered_from_a_real_charge():
    """The schedule withholds the rate in the API docs; recover it from the actual charge.
    0.0147 / (0.30 * 0.70) == 0.07 exactly."""
    assert 0.014700 / (0.30 * 0.70) == pytest.approx(0.07, abs=1e-9)


def test_fee_is_symmetric_around_half_and_peaks_there():
    """p(1-p) is symmetric and maximal at 0.5 — a sanity property of the schedule's formula."""
    assert _kalshi_taker_fee(0.30) == pytest.approx(_kalshi_taker_fee(0.70))
    assert _kalshi_taker_fee(0.50) >= _kalshi_taker_fee(0.30)


def test_edge_uses_the_corrected_fee_and_is_no_longer_understated():
    """The consequence that matters: check_kalshi_arb must reflect the real fee. With the old
    cent-ceil an edge at these prices was understated by ~0.5c — enough to fail a 2% net floor."""
    poly, kalshi = 0.45, 0.52
    edge = check_kalshi_arb(poly, kalshi, 0.0)
    expected = 1.0 - poly - (kalshi + _kalshi_taker_fee(kalshi))
    assert edge == pytest.approx(expected, abs=1e-12)
    old_fee = math.ceil(0.07 * 0.52 * 0.48 * 100) / 100
    old_edge = 1.0 - poly - (kalshi + old_fee)
    assert edge > old_edge, "corrected fee must raise the edge (fee was overstated)"


def test_zero_and_extreme_prices_do_not_explode():
    for p in (0.0001, 0.01, 0.99, 0.9999):
        f = _kalshi_taker_fee(p)
        assert 0.0 <= f < 0.02
