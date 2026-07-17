"""Tests for bot/kalshi/settlement.py — the cross-venue settlement classifier.

Pure `classify` + `realized_pnl` are exercised exhaustively (no I/O); the thin
`fetch_settlement` wrapper is tested with mocked clients (never live endpoints,
per CLAUDE.md). The classification mirrors the two ungated tails documented in
matcher.py:42-55 — VOID (either venue fair-value marked) and DIVERGENCE (venues
declare opposite definite winners → hedge breaks).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.kalshi.settlement import (
    Outcome, SettlementResult, classify, realized_pnl, realized_pnl_live, fetch_settlement,
)


# ── classify: CLEAN (hedge settled as designed — exactly one leg pays $1) ──────

def test_clean_poly_long_won_kalshi_complement_lost():
    # Poly long team_a WON (sp=1.0); our Kalshi leg was the complement side ("no")
    # and the winning side was "yes" → kalshi pays 0. Total payout = 1 → CLEAN.
    r = classify(poly_settlement_price=1.0, is_short=False,
                 kalshi_result="yes", kalshi_side="no")
    assert r.outcome is Outcome.CLEAN
    assert r.poly_payout == pytest.approx(1.0)
    assert r.kalshi_payout == pytest.approx(0.0)


def test_clean_poly_long_lost_kalshi_complement_won():
    # Poly long LOST (sp=0.0); our Kalshi "no" leg WON (result "no"). Total = 1 → CLEAN.
    r = classify(poly_settlement_price=0.0, is_short=False,
                 kalshi_result="no", kalshi_side="no")
    assert r.outcome is Outcome.CLEAN
    assert r.poly_payout == pytest.approx(0.0)
    assert r.kalshi_payout == pytest.approx(1.0)


def test_clean_short_token_flips_payout():
    # Short token: sp=0.0 means the LONG side lost → the SHORT side we hold WON → payout 1.
    # Kalshi "yes" leg lost (result "no") → total = 1 → CLEAN. Confirms short flip.
    r = classify(poly_settlement_price=0.0, is_short=True,
                 kalshi_result="no", kalshi_side="yes")
    assert r.outcome is Outcome.CLEAN
    assert r.poly_payout == pytest.approx(1.0)


# ── classify: DIVERGENCE (venues declare opposite definite winners) ────────────

def test_divergence_both_legs_lose():
    # Poly long lost (0.0) AND our Kalshi side also lost (result != side) → total 0 → DIVERGENCE.
    r = classify(poly_settlement_price=0.0, is_short=False,
                 kalshi_result="yes", kalshi_side="no")
    assert r.outcome is Outcome.DIVERGENCE


def test_divergence_both_legs_win():
    # Poly long won (1.0) AND our Kalshi side also won (result == side) → total 2 → DIVERGENCE.
    r = classify(poly_settlement_price=1.0, is_short=False,
                 kalshi_result="no", kalshi_side="no")
    assert r.outcome is Outcome.DIVERGENCE


# ── classify: VOID (either venue non-definite / fair-value marked) ─────────────

def test_void_poly_lfmp_intermediate():
    # Poly settlement strictly between 0 and 1 = LFMP fair-value void mark.
    r = classify(poly_settlement_price=0.43, is_short=False,
                 kalshi_result="yes", kalshi_side="no")
    assert r.outcome is Outcome.VOID


def test_void_kalshi_no_definite_result():
    # Kalshi settled but result is neither yes nor no (cancelled / fair-marked).
    r = classify(poly_settlement_price=1.0, is_short=False,
                 kalshi_result="", kalshi_side="no")
    assert r.outcome is Outcome.VOID


# ── classify: PENDING (not both settled — a skip, never a result) ──────────────

def test_pending_when_poly_not_settled():
    r = classify(poly_settlement_price=None, is_short=False,
                 kalshi_result="yes", kalshi_side="no",
                 poly_settled=False, kalshi_settled=True)
    assert r.outcome is Outcome.PENDING


def test_pending_when_kalshi_not_settled():
    r = classify(poly_settlement_price=1.0, is_short=False,
                 kalshi_result="", kalshi_side="no",
                 poly_settled=True, kalshi_settled=False)
    assert r.outcome is Outcome.PENDING


# ── realized_pnl ──────────────────────────────────────────────────────────────

def test_realized_pnl_clean_is_booked_edge():
    r = classify(1.0, False, "yes", "no")            # CLEAN, p_pay=1 k_pay=0
    # 10 shares, effective costs 0.30 + 0.68 → pnl = 10*1 - 10*0.98 = 0.20
    assert realized_pnl(r, shares=10, poly_eff=0.30, kalshi_eff=0.68) == pytest.approx(0.20)


def test_realized_pnl_divergence_loses_full_stake():
    r = classify(0.0, False, "yes", "no")            # DIVERGENCE, both lose
    # 10 shares: payout 0, paid 9.80 → -9.80
    assert realized_pnl(r, shares=10, poly_eff=0.30, kalshi_eff=0.68) == pytest.approx(-9.80)


def test_realized_pnl_void_is_none_not_zero():
    # Void P&L depends on two fair-value marks the backtest models (W_VOID); the
    # module must NOT silently return 0 (that's the inverted blind-to-losses bug).
    r = classify(0.5, False, "yes", "no")            # VOID
    assert realized_pnl(r, shares=10, poly_eff=0.30, kalshi_eff=0.68) is None


# ── classify with the Kalshi void mark (settlement_value_dollars) ──────────────

def test_classify_void_fills_kalshi_mark_from_value_no_side():
    # Kalshi result='scalar' (non-definite) + YES-side fair value 0.60; held NO → 1-0.60=0.40.
    r = classify(0.0, False, "scalar", "no", kalshi_value=0.60)
    assert r.outcome is Outcome.VOID and r.kalshi_payout == pytest.approx(0.40)


def test_classify_void_fills_kalshi_mark_from_value_yes_side():
    r = classify(0.0, False, "scalar", "yes", kalshi_value=0.60)
    assert r.outcome is Outcome.VOID and r.kalshi_payout == pytest.approx(0.60)


def test_classify_void_kalshi_mark_absent_leaves_payout_none():
    # No settlement_value_dollars → no mark → kalshi_payout stays None → live floor downstream.
    r = classify(1.0, False, "scalar", "no", kalshi_value=None)
    assert r.outcome is Outcome.VOID and r.kalshi_payout is None


# ── realized_pnl_live (the LIVE writer's P&L: resolves VOID to a number) ────────

def test_live_clean_matches_booked_edge():
    r = classify(1.0, False, "yes", "no")
    assert realized_pnl_live(r, 10, 0.30, 0.68, w_void=0.10) == pytest.approx(0.20)


def test_live_divergence_full_stake_loss():
    r = classify(0.0, False, "yes", "no")
    assert realized_pnl_live(r, 10, 0.30, 0.68, w_void=0.10) == pytest.approx(-9.80)


def test_live_void_with_both_marks_is_actual_pnl():
    # Poly LFMP 0.30 + Kalshi fair (held NO) 0.40 → payout 7.0 − cost 9.80 = -2.80.
    r = classify(0.30, False, "scalar", "no", kalshi_value=0.60)
    assert realized_pnl_live(r, 10, 0.30, 0.68, w_void=0.10) == pytest.approx(-2.80)


def test_live_void_missing_mark_floors_to_w_void_never_zero():
    # Kalshi mark absent → must floor to -shares*w_void, NOT zero (condition 4b).
    r = classify(1.0, False, "scalar", "no", kalshi_value=None)
    out = realized_pnl_live(r, 10, 0.30, 0.68, w_void=0.10)
    assert out == pytest.approx(-1.0) and out != 0.0


def test_live_pending_is_none():
    r = classify(None, False, "", "no", poly_settled=False)
    assert realized_pnl_live(r, 10, 0.30, 0.68, w_void=0.10) is None


def test_live_and_offline_pnl_identical_on_clean_and_divergence():
    # The cap (realized_pnl_live) and the backtest (realized_pnl) MUST agree on the cases both
    # handle — clean + divergence — so measurement and live can't silently drift apart. The live
    # one only ADDITIONALLY resolves void. Same inputs → same number.
    for r in (classify(1.0, False, "yes", "no"),     # CLEAN
              classify(0.0, False, "yes", "no")):    # DIVERGENCE (both lose)
        off = realized_pnl(r, shares=10, poly_eff=0.30, kalshi_eff=0.68)
        live = realized_pnl_live(r, 10, 0.30, 0.68, w_void=0.10)
        assert off is not None and live == pytest.approx(off)


# ── fetch_settlement wrapper (mocked clients, no live calls) ───────────────────

@pytest.mark.asyncio
async def test_fetch_settlement_clean_mlb():
    poly = MagicMock()
    poly.get_settlement = AsyncMock(return_value=1.0)         # long-side settlement price
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value={"market": {"status": "finalized", "result": "yes"}})
    r = await fetch_settlement(poly, kalshi, "aec-mlb-pit-col-2026-06-21", "KXMLBGAME-X", "no")
    assert r.outcome is Outcome.CLEAN


@pytest.mark.asyncio
async def test_fetch_settlement_pending_when_kalshi_active():
    poly = MagicMock()
    poly.get_settlement = AsyncMock(return_value=None)
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value={"market": {"status": "active", "result": ""}})
    r = await fetch_settlement(poly, kalshi, "aec-mlb-pit-col-2026-06-21::short", "KXMLBGAME-X", "yes")
    assert r.outcome is Outcome.PENDING


@pytest.mark.asyncio
async def test_fetch_settlement_short_token_parsed():
    poly = MagicMock()
    poly.get_settlement = AsyncMock(return_value=0.0)        # long lost → short (held) won
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value={"market": {"status": "settled", "result": "no"}})
    r = await fetch_settlement(poly, kalshi, "aec-mlb-pit-col-2026-06-21::short", "KXMLBGAME-X", "yes")
    # short won (payout 1) + kalshi "yes" leg lost (result "no") → total 1 → CLEAN
    assert r.outcome is Outcome.CLEAN
    assert r.poly_payout == pytest.approx(1.0)
