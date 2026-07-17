"""
bot/core/matcher.py
───────────────────
Team-name normalization + matching helpers, shared across venue pairs.

This is the NAME-RESOLUTION layer, not a matcher itself. The live cross-venue matching
(Kalshi ↔ Polymarket US) lives in bot/kalshi/matcher.py; it calls the helpers here to decide
whether two venues' team labels refer to the same team. (The legacy sportsbook OddsEvent matcher
this file once held is dead.)

Live helpers:
  fetch_team_normalization() — build an abbreviation/alias lookup from the Gamma /teams endpoint.
  _extract_team_name()       — strip the " wins" suffix from a Poly question ("Lakers wins" → "Lakers").
  _names_match()             — match a name across venues: exact (case-insensitive) → normalization
                               table → MLB split-city disambiguation (_mlb_canon) → substring.
  parse_iso()                — parse an ISO-8601 timestamp (game-time filtering in the Kalshi matcher).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from bot.core import config
from bot.core.logger import get_logger
from bot.core.types import MarketPair

log = get_logger(__name__)

_TEAMS_URL = f"{config.GAMMA_HOST}/teams"



# ── Team normalization ─────────────────────────────────────────────────────────

def fetch_team_normalization() -> dict[str, str]:
    """
    Fetch team data from Gamma API and build a short-name → full-name lookup.
    Covers: name, abbreviation, and any aliases listed in the team object.

    Returns empty dict on error — bot continues without normalization.
    """
    try:
        resp = requests.get(_TEAMS_URL, timeout=15)
        resp.raise_for_status()
        teams = resp.json()
    except Exception as exc:
        log.warning(f"Matcher: failed to fetch /teams from Gamma: {exc}")
        return {}

    norm: dict[str, str] = {}
    for team in teams:
        full_name = team.get("name", "")
        if not full_name:
            continue
        # Map the full name itself
        norm[full_name.lower()] = full_name
        # Map abbreviation
        abbr = team.get("abbreviation") or team.get("abbr") or ""
        if abbr:
            norm[abbr.lower()] = full_name
        # Map each alias
        for alias in team.get("aliases", []) or []:
            if alias:
                norm[str(alias).lower()] = full_name

    log.info(f"Matcher: built normalization table with {len(norm)} entries")
    return norm


# ── Name helpers ───────────────────────────────────────────────────────────────

def _extract_team_name(question: str) -> str:
    """
    Strip trailing " wins" (case-insensitive) from a market question
    to recover the bare team name.
    e.g. "Lakers wins" → "Lakers", "Will Lakers win?" → "Will Lakers win?"
    """
    q = question.strip()
    if q.lower().endswith(" wins"):
        return q[:-5].strip()
    if q.lower().endswith(" win"):
        return q[:-4].strip()
    return q


def _normalize(name: str, norm_table: dict[str, str]) -> str:
    """Return the canonical full name for name, or name itself if not found."""
    return norm_table.get(name.lower(), name)


# ── MLB team table ──────────────────────────────────────────────────────────────
# Poly US uses full names ("Toronto Blue Jays"); Kalshi uses short labels with
# explicit suffixes for shared cities ("Chicago C"/"Chicago WS", "Los Angeles A"/
# "Los Angeles D", "New York M"/"New York Y", "A's"). Substring matching can't tell
# the same-city teams apart, so map BOTH naming styles to one canonical token and
# require an exact canonical match. MLB is a fixed 30-team league — an explicit
# table is the deterministic, low-risk choice.
#
# NOTE (public snapshot): the table below is an ILLUSTRATIVE subset that keeps every
# shared-city disambiguation pair (Chicago C/WS, New York M/Y, Los Angeles A/D — the
# whole reason this table exists) plus a representative spread. The full 30-team
# production table is maintained privately and merged in via an optional gitignored
# overlay when present (see NOTICE.md); absent, matching is limited to the teams here.
_MLB_TEAMS: dict[str, tuple[str, str]] = {
    # canonical: (Poly full name, Kalshi short label)
    "ath": ("athletics", "a's"),
    "bos": ("boston red sox", "boston"),
    "chc": ("chicago cubs", "chicago c"),
    "cws": ("chicago white sox", "chicago ws"),
    "cle": ("cleveland guardians", "cleveland"),
    "det": ("detroit tigers", "detroit"),
    "laa": ("los angeles angels", "los angeles a"),
    "lad": ("los angeles dodgers", "los angeles d"),
    "mil": ("milwaukee brewers", "milwaukee"),
    "min": ("minnesota twins", "minnesota"),
    "nym": ("new york mets", "new york m"),
    "nyy": ("new york yankees", "new york y"),
    "pit": ("pittsburgh pirates", "pittsburgh"),
    "tor": ("toronto blue jays", "toronto"),
    "wsh": ("washington nationals", "washington"),
}
# Optional private overlay: the full production team table is loaded from a (gitignored)
# module if present, replacing the illustrative subset above. Absent → no-op. See NOTICE.md.
try:  # pragma: no cover — overlay is optional and absent in the public tree
    from bot.core._private_teams import MLB_TEAMS_FULL  # type: ignore
    _MLB_TEAMS = MLB_TEAMS_FULL
except Exception:  # noqa: BLE001 — optional overlay, never fatal
    pass
# Reverse lookup: any known name spelling (lowercased) → canonical token.
_MLB_NAME_TO_CANON: dict[str, str] = {
    name: canon for canon, names in _MLB_TEAMS.items() for name in names
}


def _mlb_canon(name: str) -> str | None:
    """Return the canonical MLB token for a Poly full name or Kalshi label, else None."""
    return _MLB_NAME_TO_CANON.get(name.strip().lower())


def _names_match(poly_name: str, book_name: str, norm_table: dict[str, str]) -> bool:
    """
    Return True if poly_name and book_name refer to the same team.
    Tries in order:
      0. MLB canonical table (exact — disambiguates shared-city teams)
      1. Exact case-insensitive
      2. Norm-table lookup for poly_name, then compare
      3. Substring containment
    """
    # 0. MLB: if BOTH names are known MLB teams, require an exact canonical match.
    # Returning here (not falling through) stops substring from cross-matching
    # same-city teams (e.g. "Chicago Cubs" vs Kalshi "Chicago WS").
    poly_canon = _mlb_canon(poly_name)
    book_canon = _mlb_canon(book_name)
    if poly_canon is not None and book_canon is not None:
        return poly_canon == book_canon
    # 1. Exact
    if poly_name.lower() == book_name.lower():
        return True
    # 2. Normalize poly side and compare
    resolved = _normalize(poly_name, norm_table)
    if resolved.lower() == book_name.lower():
        return True
    # 3. Substring (e.g. "Lakers" in "Los Angeles Lakers")
    p = poly_name.lower()
    b = book_name.lower()
    if p in b or b.endswith(p):
        return True
    # Also try normalized poly name as substring
    r = resolved.lower()
    if r in b or b == r:
        return True
    return False


# ── Time helpers ───────────────────────────────────────────────────────────────

def parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO8601 timestamp, return None on failure.

    Public utility: also used by cross_arb.py for commence_time and bookmaker
    last_update / unchanged_since timestamps. Handles the trailing-Z form
    that Python's fromisoformat rejects pre-3.11.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
