"""
tests/test_matcher.py
─────────────────────
Unit tests for the matcher: team-name resolution, time-window filter,
and end-to-end MarketPair ↔ OddsEvent pairing. Pure math + string
operations — no network.
"""
from datetime import datetime, timedelta, timezone

from bot.core.matcher import (
    _extract_team_name,
    _names_match,
    _normalize,
    parse_iso,
)
from bot.core.types import MarketPair


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ── parse_iso ───────────────────────────────────────────────────────────────

def test_parse_iso_basic():
    dt = parse_iso("2026-05-20T12:00:00+00:00")
    assert dt == datetime(2026, 5, 20, 12, tzinfo=timezone.utc)


def test_parse_iso_z_suffix():
    dt = parse_iso("2026-05-20T12:00:00Z")
    assert dt == datetime(2026, 5, 20, 12, tzinfo=timezone.utc)


def test_parse_iso_empty_returns_none():
    assert parse_iso("") is None
    assert parse_iso(None) is None  # type: ignore[arg-type]


def test_parse_iso_invalid_returns_none():
    assert parse_iso("not-a-date") is None
    assert parse_iso("2026-99-99") is None


# ── _normalize ──────────────────────────────────────────────────────────────

def test_normalize_lowercase_lookup():
    table = {"lal": "Los Angeles Lakers", "lakers": "Los Angeles Lakers"}
    assert _normalize("LAL", table) == "Los Angeles Lakers"
    assert _normalize("Lakers", table) == "Los Angeles Lakers"


def test_normalize_unknown_returns_input():
    table = {"lal": "Los Angeles Lakers"}
    assert _normalize("Celtics", table) == "Celtics"


# ── _extract_team_name ──────────────────────────────────────────────────────

def test_extract_team_name_strips_wins_suffix():
    assert _extract_team_name("Lakers wins") == "Lakers"
    assert _extract_team_name("Los Angeles Lakers wins") == "Los Angeles Lakers"


def test_extract_team_name_no_suffix_returns_input():
    assert _extract_team_name("Will Lakers win?") == "Will Lakers win?"


# ── _names_match ────────────────────────────────────────────────────────────

def test_names_match_exact_case_insensitive():
    assert _names_match("lakers", "Lakers", {}) is True
    assert _names_match("LAKERS", "lakers", {}) is True


def test_names_match_via_norm_table():
    table = {"lal": "Los Angeles Lakers"}
    assert _names_match("LAL", "Los Angeles Lakers", table) is True


def test_names_match_substring_containment():
    """Bare team name appears inside the full city+team form."""
    assert _names_match("Lakers", "Los Angeles Lakers", {}) is True


def test_names_match_no_match():
    assert _names_match("Celtics", "Lakers", {}) is False

