"""Tests for the macro cross-platform arb allowlist + validation."""
from unittest.mock import MagicMock, patch

import pytest

from bot.kalshi.macro_pairs import MacroPair, _resolve_poly_tokens


# ── _resolve_poly_tokens ──────────────────────────────────────────────────────

def test_resolve_poly_tokens_returns_token_list():
    fake_resp = MagicMock()
    fake_resp.json.return_value = [
        {"conditionId": "0xabc", "clobTokenIds": '["tok_yes", "tok_no"]'}
    ]
    fake_resp.raise_for_status.return_value = None
    with patch("bot.kalshi.macro_pairs.requests.get", return_value=fake_resp) as g:
        tokens = _resolve_poly_tokens("0xabc")
    g.assert_called_once()
    assert tokens == ["tok_yes", "tok_no"]


def test_resolve_poly_tokens_unknown_condition_returns_none():
    fake_resp = MagicMock()
    fake_resp.json.return_value = []
    fake_resp.raise_for_status.return_value = None
    with patch("bot.kalshi.macro_pairs.requests.get", return_value=fake_resp):
        assert _resolve_poly_tokens("0xmissing") is None


def test_resolve_poly_tokens_network_error_returns_none():
    with patch("bot.kalshi.macro_pairs.requests.get", side_effect=Exception("boom")):
        assert _resolve_poly_tokens("0xabc") is None


# ── validate_macro_pairs helpers ──────────────────────────────────────────────

def _good_pair(**overrides):
    base = dict(
        poly_condition_id="0xabc",
        poly_token="tok_yes",
        kalshi_ticker="KXFED-26JUN-C25",
        kalshi_side="yes",
        comment="Poly YES 'Fed cuts in June' vs Kalshi YES 'Fed cuts >=25bps'",
        category="fed",
    )
    base.update(overrides)
    return MacroPair(**base)


# A live Kalshi market dict for the good ticker: binary (no strikes), open.
_LIVE_KALSHI = {
    "KXFED-26JUN-C25": {
        "ticker": "KXFED-26JUN-C25",
        "status": "open",
        "floor_strike": None,
        "cap_strike": None,
        "yes_bid_dollars": 0.40,
        "yes_ask_dollars": 0.42,
    },
}


def _patch_poly_ok():
    return patch("bot.kalshi.macro_pairs._resolve_poly_tokens",
                 return_value=["tok_yes", "tok_no"])


# ── schema checks ─────────────────────────────────────────────────────────────

def test_validate_accepts_good_pair():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair()], _LIVE_KALSHI)
    assert len(valid) == 1
    assert valid[0].kalshi_ticker == "KXFED-26JUN-C25"


def test_validate_rejects_bad_side():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair(kalshi_side="maybe")], _LIVE_KALSHI)
    assert valid == []


def test_validate_rejects_bad_category():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair(category="elections")], _LIVE_KALSHI)
    assert valid == []


def test_validate_rejects_empty_comment():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair(comment="  ")], _LIVE_KALSHI)
    assert valid == []


def test_validate_rejects_empty_identity_field():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair(poly_token="")], _LIVE_KALSHI)
    assert valid == []


def test_validate_skips_only_bad_pair_keeps_rest():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    pairs = [_good_pair(kalshi_side="maybe"), _good_pair()]
    with _patch_poly_ok():
        valid = validate_macro_pairs(pairs, _LIVE_KALSHI)
    assert len(valid) == 1


# ── Kalshi scope-boundary + existence checks ──────────────────────────────────

def test_validate_rejects_unknown_kalshi_ticker():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair(kalshi_ticker="KXFED-NOPE")], _LIVE_KALSHI)
    assert valid == []


def test_validate_rejects_closed_kalshi_market():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    closed = {"KXFED-26JUN-C25": {
        "ticker": "KXFED-26JUN-C25",
        "status": "closed",
        "floor_strike": None,
        "cap_strike": None,
    }}
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair()], closed)
    assert valid == []


def test_validate_accepts_threshold_market_with_floor_strike():
    """Threshold markets (floor_strike set, no cap) are binary YES/NO and must be accepted."""
    from bot.kalshi.macro_pairs import validate_macro_pairs
    threshold = {"KXFED-26JUN-C25": {
        "ticker": "KXFED-26JUN-C25",
        "status": "open",
        "floor_strike": 4.25,
        "cap_strike": None,
    }}
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair()], threshold)
    assert len(valid) == 1


def test_validate_rejects_range_bucket_market():
    """Scope boundary: a both-strikes (range bucket) market must be rejected."""
    from bot.kalshi.macro_pairs import validate_macro_pairs
    bucket = {"KXFED-26JUN-C25": {
        "ticker": "KXFED-26JUN-C25",
        "status": "open",
        "floor_strike": 25.0,
        "cap_strike": 50.0,
    }}
    with _patch_poly_ok():
        valid = validate_macro_pairs([_good_pair()], bucket)
    assert valid == []


# ── Polymarket existence checks ───────────────────────────────────────────────

def test_validate_rejects_unresolved_poly_condition():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    with patch("bot.kalshi.macro_pairs._resolve_poly_tokens", return_value=None):
        valid = validate_macro_pairs([_good_pair()], _LIVE_KALSHI)
    assert valid == []


def test_validate_rejects_token_not_in_poly_market():
    from bot.kalshi.macro_pairs import validate_macro_pairs
    with patch("bot.kalshi.macro_pairs._resolve_poly_tokens",
               return_value=["other_a", "other_b"]):
        valid = validate_macro_pairs([_good_pair(poly_token="tok_yes")], _LIVE_KALSHI)
    assert valid == []
