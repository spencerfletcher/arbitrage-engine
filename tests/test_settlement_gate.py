"""Tests for the settlement-equivalence gate (bot/kalshi/matcher.is_settlement_equivalent).

A cross-exchange hedge only pays a guaranteed $1 if Poly-YES and Kalshi-NO cover
identical outcome sets. This gate fail-closes: only (Kalshi series × Poly settlement
structure) pairs VERIFIED equivalent are tradeable. Everything else is blocked.
"""
from bot.kalshi.matcher import is_settlement_equivalent


def test_wc_group_stage_drawable_is_equivalent():
    # WC game markets settle both sides on 90'+stoppage, draw valid → equivalent.
    assert is_settlement_equivalent("KXWCGAME-26JUN17GHAPAN-GHA", "drawable_outcome") is True


def test_wc_non_drawable_blocked():
    # A WC market that isn't the drawable (w/-tie) structure → not verified → block.
    assert is_settlement_equivalent("KXWCGAME-26JUN17GHAPAN-GHA", "winner") is False


def test_unknown_settlement_type_blocked():
    # Empty/unknown settlement type (e.g. global Poly scanner that doesn't set it).
    assert is_settlement_equivalent("KXWCGAME-26JUN17GHAPAN-GHA", "") is False


def test_mlb_moneyline_is_equivalent():
    # MLB: both venues settle on the full-game winner (incl extra innings), no draw.
    assert is_settlement_equivalent("KXMLBGAME-26JUN18TORBOS-TOR", "moneyline") is True


def test_wnba_moneyline_is_equivalent():
    # WNBA: verified 2026-06-23 from live rules text — both venues settle on the official game
    # winner (OT included, no draw), same scheduled game; void → fair-value on both.
    assert is_settlement_equivalent("KXWNBAGAME-26JUN24PHXIND-PHX", "moneyline") is True


def test_nba_moneyline_is_equivalent():
    # NBA: allowlisted on the SAME basis as WNBA (winner incl OT, no draw). Seasonally inactive
    # now (no live games until ~Oct) but blessed for coherence + zero-cost next-season readiness.
    assert is_settlement_equivalent("KXNBAGAME-LAKCEL-JUN14", "moneyline") is True


def test_nhl_moneyline_is_equivalent():
    # NHL: verified 2026-07-14 from both venues' rules text — same winner basis, and NHL cannot draw
    # (OT + shootout always decide). Poly's NHL moneyline template is verbatim the NBA one (postponed
    # → stays open; canceled outright, no make-up → 50-50). Seasonally inactive until ~Oct.
    assert is_settlement_equivalent("KXNHLGAME-26JUN15EDMFLA-EDM", "moneyline") is True


def test_other_series_blocked_until_verified():
    # Basketball/hockey are moneyline-only — never the soccer drawable structure.
    assert is_settlement_equivalent("KXNBAGAME-LAKCEL-JUN14", "drawable_outcome") is False
    assert is_settlement_equivalent("KXWNBAGAME-26JUN24PHXIND-PHX", "drawable_outcome") is False
    assert is_settlement_equivalent("KXNHLGAME-26JUN15EDMFLA-EDM", "drawable_outcome") is False
    # MLB only verified for the moneyline structure, not drawable.
    assert is_settlement_equivalent("KXMLBGAME-PITATH-JUL07", "drawable_outcome") is False
    # Fail-closed on an unverified series. Tennis researched 2026-07-14 and REJECTED: Kalshi resolves
    # a mid-match retirement to the winner ("after a ball has been played") while Poly's
    # tennis_match_winner rules are silent on retirement (~4-7% of matches) → equivalence unprovable.
    assert is_settlement_equivalent("KXATPMATCH-26JUL15TRUDAV-TRU", "moneyline") is False


def test_malformed_ticker_blocked():
    assert is_settlement_equivalent("", "drawable_outcome") is False
