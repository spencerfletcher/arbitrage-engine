"""Tests for the edge-proportional Kalshi profit floor (`_fok_buffer`) and its ONE reader,
`fire_limits` — which is where the buffer's reach is decided, and so where a regression would
actually change prices on the wire."""
import dataclasses

import pytest
from bot.core import config
from bot.kalshi.cross_arb import KalshiArbOpportunity, _effective_share_cost
from bot.runner.common import _fok_buffer
from bot.runner.kalshi_arb import fire_limits


def _opp(poly_ask_raw=0.26, kalshi_ask=0.68, edge=0.0304, tick=0.01):
    poly_eff = _effective_share_cost(poly_ask_raw, 0.06)
    o = KalshiArbOpportunity(
        event_title="A vs B", poly_event_id="ev1", poly_token="tok", poly_team="A",
        poly_ask_raw=poly_ask_raw, poly_ask=poly_eff, kalshi_ticker="KX-A", kalshi_side="no",
        kalshi_team="A", kalshi_ask=kalshi_ask, edge=edge, shares=7,
        total_cost=7 * (poly_eff + kalshi_ask), guaranteed_profit=7 * edge)
    o.kalshi_tick = tick
    return o


def test_fat_edge_scales_buffer(monkeypatch):
    monkeypatch.setattr(config, "KALSHI_FOK_BUFFER", 0.005)
    monkeypatch.setattr(config, "KALSHI_FOK_EDGE_FRACTION", 0.25)
    # 36% edge → 0.36 * 0.25 / 2 = 0.045. Spent entirely on the Kalshi limit; see fire_limits.
    assert _fok_buffer(0.36) == pytest.approx(0.045)


def test_thin_edge_uses_fixed_floor(monkeypatch):
    monkeypatch.setattr(config, "KALSHI_FOK_BUFFER", 0.005)
    monkeypatch.setattr(config, "KALSHI_FOK_EDGE_FRACTION", 0.25)
    # 5% edge → 0.05*0.125 = 0.00625 > 0.005 floor
    assert _fok_buffer(0.05) == pytest.approx(0.00625)


def test_buffer_capped_to_stay_profitable(monkeypatch):
    monkeypatch.setattr(config, "KALSHI_FOK_BUFFER", 0.05)  # large fixed floor
    monkeypatch.setattr(config, "KALSHI_FOK_EDGE_FRACTION", 0.25)
    # tiny edge: floor 0.05 would exceed edge/2 → cap at 0.45*edge keeps it profitable
    assert _fok_buffer(0.02) == pytest.approx(0.009)  # 0.45 * 0.02
    # ⚠️ The cap keeps buf < edge/2, which is what this asserts — but do NOT read it as
    # "combined slippage = 2*buf < edge". That model is WRONG (corrected 2026-07-15) and there is
    # no "per leg" at all any more (S4, 2026-07-16): buf reaches ONLY the Kalshi limit, which is
    # breakeven-MINUS-buf, so a bigger buf TIGHTENS it. This line previously carried the false
    # invariant as its comment, which would have "protected" the wrong model on any future change.
    assert 2 * _fok_buffer(0.02) < 0.02  # the 0.45*edge cap holds — NOT a claim about slippage


# ---- fire_limits: the buffer's reach. These pin the asymmetry, not the arithmetic. ----

def test_poly_limit_is_the_raw_ask_with_NO_cushion(monkeypatch):
    """S4: the Poly leg bids the ask FLAT. Measured to rescue 0/14 poly_moved states — the
    closest miss was 2.3c against a ~0.005 cushion, i.e. 4.6x too far. Kills any `+ buf`,
    `+ tick`, or ceil-to-tick creeping back onto the BUY leg."""
    monkeypatch.setattr(config, "KALSHI_FOK_BUFFER", 0.005)
    monkeypatch.setattr(config, "KALSHI_FOK_EDGE_FRACTION", 0.35)
    poly_limit, _ = fire_limits(_opp(poly_ask_raw=0.26))
    assert poly_limit == pytest.approx(0.26)


def test_poly_limit_ignores_the_buffer_knobs_entirely(monkeypatch):
    """The strong form: the buy price must not move when the buffer is cranked to absurdity.
    An `== approx(raw)` alone would still pass a cushion that happened to floor away on this
    market's tick — which is exactly how the old Poly half hid for months (a no-op on 0.01,
    a full 1-tick cushion on 0.005, and the live slate is ~50/50 both)."""
    monkeypatch.setattr(config, "KALSHI_FOK_EDGE_FRACTION", 0.0)
    for floor in (0.0, 0.005, 0.05):
        monkeypatch.setattr(config, "KALSHI_FOK_BUFFER", floor)
        poly_limit, _ = fire_limits(_opp(poly_ask_raw=0.435, edge=0.10))
        assert poly_limit == pytest.approx(0.435), f"buffer {floor} leaked onto the Poly leg"


def test_kalshi_limit_still_carries_the_whole_buffer_and_TIGHTENS(monkeypatch):
    """The other half of S4: dropping the Poly cushion must not disarm the Kalshi profit floor.
    Raising buf must LOWER the Kalshi limit (demand more profit), never raise it."""
    monkeypatch.setattr(config, "KALSHI_FOK_EDGE_FRACTION", 0.0)
    monkeypatch.setattr(config, "KALSHI_FOK_BUFFER", 0.005)
    _, tight_floor = fire_limits(_opp(edge=0.10, tick=0.001))
    monkeypatch.setattr(config, "KALSHI_FOK_BUFFER", 0.02)
    _, big_floor = fire_limits(_opp(edge=0.10, tick=0.001))
    assert big_floor < tight_floor, "a bigger buf must TIGHTEN the Kalshi limit"
    # ~0.015 = the buffer delta, passed through 1:1 — but each limit floors to the tick
    # independently, so allow one tick of quantization. (Exact equality fails at 0.016 here.)
    assert abs((tight_floor - big_floor) - 0.015) <= 0.001 + 1e-9


def test_kalshi_limit_floors_to_the_venue_tick(monkeypatch):
    """Kalshi rejects off-tick, so its limit must floor DOWN (paying less is always safe)."""
    monkeypatch.setattr(config, "KALSHI_FOK_EDGE_FRACTION", 0.0)
    monkeypatch.setattr(config, "KALSHI_FOK_BUFFER", 0.005)
    _, kalshi_limit = fire_limits(_opp(tick=0.01))
    assert kalshi_limit == pytest.approx(round(kalshi_limit, 2))
    _, unfloored = fire_limits(_opp(tick=0.001))
    assert kalshi_limit <= unfloored
