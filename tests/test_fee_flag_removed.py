"""The Poly fee must not be silenceable by a flag that defaults to OFF (S7).

`MarketPair.fees_enabled` defaulted to False and cross_arb read
`taker_fee_rate if fees_enabled else 0.0` — so a construction site that forgot the flag silently
zeroed the fee, overstating every Poly edge by up to 1.5c/share and firing trades never above the
2% floor. Silent, on the money path.

It also contradicted the guard two files away. `_coerce_fee_rate` exists to prevent exactly this and
says so: "a missing / null / non-numeric / non-positive value falls back to _POLY_US_TAKER_THETA,
NEVER 0 — a zero fee would under-cost every Poly edge and over-size the position." That fails
CLOSED. fees_enabled failed OPEN. Two guards on one value, opposite fail directions.

taker_fee_rate is already self-fail-safe, so it is read directly now.
"""
import pytest

from bot.core.types import MarketPair


def _pair(**kw):
    base = dict(
        event_id="ev1", event_title="A vs B",
        token_yes_a="tok-a", token_no_a="", question_a="A?",
        token_yes_b="tok-b", token_no_b="", question_b="B?",
        taker_fee_rate=0.06,
    )
    base.update(kw)
    return MarketPair(**base)


def test_a_pair_that_never_mentions_a_fee_flag_still_carries_the_fee():
    """The regression guard: no flag to forget."""
    p = _pair()
    assert p.taker_fee_rate == 0.06
    assert not hasattr(p, "fees_enabled"), "fees_enabled must be gone — it fails OPEN (silent 0 fee)"


def test_the_dead_exponent_field_is_gone():
    """taker_fee_exponent: never set by any scanner, never read by anything. Its comment
    ("1 for sports, 2 for crypto") described the global-Poly schedule; the live formula is
    Theta*p*(1-p), exponent-free."""
    assert not hasattr(_pair(), "taker_fee_exponent")


def test_the_edge_math_charges_the_fee_without_any_flag():
    """cross_arb must read taker_fee_rate directly. If it still gated on a flag, this pair would
    price fee-free and every edge would be overstated."""
    from bot.kalshi.cross_arb import _effective_share_cost
    p = _pair()
    assert _effective_share_cost(0.50, p.taker_fee_rate) == pytest.approx(0.515)
    assert _effective_share_cost(0.50, p.taker_fee_rate) > 0.50, "fee must be charged"
