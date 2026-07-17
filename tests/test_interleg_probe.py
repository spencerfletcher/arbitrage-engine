"""§5 inter-leg-window probe: the _interleg_kill classification (kalshi_arb).

The probe (DRY-only, observability) re-reads the Kalshi leg ~one round-trip after a would-fire and
records whether it would have killed in the window. The fire path never branches on this — but the
classification is the load-bearing input to the poly_first-vs-kalshi_first decision, so pin it.
"""
import pytest

from bot.runner.kalshi_arb import _book_levels_present, _fillable_from_book, _interleg_kill


def test_kill_when_gate_passed_then_leg_empties():
    # gate passed (t0 >= shares) but a round-trip later the leg can't fill → simulated FOK kill
    assert _interleg_kill(entry_depth_t0=10, t1=0, shares=6) == 1
    assert _interleg_kill(entry_depth_t0=10, t1=5, shares=6) == 1


def test_no_kill_when_still_fillable():
    assert _interleg_kill(entry_depth_t0=10, t1=10, shares=6) == 0
    # t1 exactly == shares is still fillable (FOK needs >= shares), not a kill
    assert _interleg_kill(entry_depth_t0=10, t1=6, shares=6) == 0


def test_no_kill_when_gate_never_passed():
    # t0 < shares would have been rejected by the entry-depth gate upstream — not a §5 event
    assert _interleg_kill(entry_depth_t0=3, t1=0, shares=6) == 0


def test_blank_not_zero_on_unknown_t1():
    # a probe read error (t1 None) must be BLANK, never 0 — a feed hiccup is not a kill
    assert _interleg_kill(entry_depth_t0=10, t1=None, shares=6) == ""


# ── _book_levels_present: the parse-miss fail-direction (fixed 2026-07-15) ──────────────────
# Before this guard, EVERY shape below parsed to 0.0 via _fillable_from_book and logged a KILL —
# the probe manufactured kills out of feed hiccups and biased §5 against poly_first. All 3 flagged
# rows in the 2026-07-14 dataset were `entry_depth_t1 == 0`, perfectly collinear with this bug.

_UNREADABLE = {
    "missing key":          {"orderbook_fp": {}},
    "orderbook_fp is None": {"orderbook_fp": None},
    "levels is None":       {"orderbook_fp": {"no_dollars": None}},
    "changed shape (v1)":   {"orderbook": {"no": [[55, 900]]}},
    "200 w/ error body":    {"error": "market_paused"},
    "not a dict":           "market_paused",
}


@pytest.mark.parametrize("label,book", list(_UNREADABLE.items()), ids=list(_UNREADABLE))
def test_unreadable_book_is_unknown_not_empty(label, book):
    """An unreadable response must read as UNKNOWN → blank, never as a kill."""
    assert _book_levels_present(book, "yes") is False
    # and composed as the probe composes it: unparseable → t1=None → blank
    t1 = _fillable_from_book(book, "yes", 0.60) if _book_levels_present(book, "yes") else None
    assert _interleg_kill(entry_depth_t0=915, t1=t1, shares=6) == "", \
        f"{label!r} must log blank, not a kill"


def test_genuinely_empty_book_is_present_and_still_a_kill():
    """An EMPTY levels array is a real observation — a real FOK would kill. Must NOT be
    suppressed by the guard (that would fail the other way and hide true kills)."""
    book = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
    assert _book_levels_present(book, "yes") is True
    t1 = _fillable_from_book(book, "yes", 0.60)
    assert t1 == 0.0
    assert _interleg_kill(entry_depth_t0=915, t1=t1, shares=6) == 1


def test_healthy_book_is_present_and_no_kill():
    book = {"orderbook_fp": {"no_dollars": [[0.45, 900.0]]}}
    assert _book_levels_present(book, "yes") is True
    t1 = _fillable_from_book(book, "yes", 0.60)
    assert t1 == 900.0
    assert _interleg_kill(entry_depth_t0=915, t1=t1, shares=6) == 0


def test_guard_reads_the_same_side_array_as_the_parser():
    """Buying `side` lifts the OPPOSING book — the guard MUST key off the same array
    _fillable_from_book reads, or it green-lights a book whose relevant side is missing."""
    yes_only = {"orderbook_fp": {"yes_dollars": [[0.55, 900.0]]}}
    assert _book_levels_present(yes_only, "no") is True    # side=no reads yes_dollars
    assert _book_levels_present(yes_only, "yes") is False  # side=yes needs no_dollars — absent
