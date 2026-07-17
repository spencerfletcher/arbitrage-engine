"""Active-game-window gate on the Poly freshness watchdog — only alarm when a game should be moving,
so off-hours / stale-listing quiet stops looking like a freeze (the 62-reconnect churn fix).

Spans: feed_health._stale_book_reconnect (the gate), common.build_game_windows (windows + the
unknown-flag — the safety crux), PolyUSOrderBookCache._books_should_move (err-toward-watching)."""
from types import SimpleNamespace as NS

from bot.core.feed_health import _stale_book_reconnect
from bot.core.matcher import parse_iso
from bot.runner.common import build_game_windows, _WINDOW_POST_BUFFER_S, _WINDOW_MISSING_END_S
from bot.poly_us.feed import PolyUSOrderBookCache


def _pair(start=None, end=None):
    return NS(start_date=start, end_date=end)


# ── the gate: _stale_book_reconnect(books_should_move=…) ───────────────────────────────────────
# stale = 200s since the last book change (> 180 threshold), 1 slug, past cooldown → would fire.
_STALE = dict(now=10_000.0, last_book_change_ts=10_000.0 - 200.0, n_subscribed=1,
              last_forced_ts=0.0, threshold=180.0, cooldown=60.0)


def test_gate_live_game_frozen_book_still_fires():
    # THE CRUX: during a live game (books_should_move=True), a frozen book STILL trips — byte-identical
    # freeze detection; the gate only ADDS an off-hours skip, never weakens a real-freeze catch mid-game.
    assert _stale_book_reconnect(**_STALE, books_should_move=True) is True


def test_gate_no_game_suppresses():
    # No game in-window → a quiet book is EXPECTED, not a freeze → SKIP (the new suppression).
    assert _stale_book_reconnect(**_STALE, books_should_move=False) is False


def test_gate_default_armed_is_kalshi_byte_identical():
    # The Kalshi feed passes no books_should_move → default True → freeze detection unchanged.
    assert _stale_book_reconnect(**_STALE) is True
    # the gate never overrides the existing n_subscribed / threshold guards:
    assert _stale_book_reconnect(**{**_STALE, "n_subscribed": 0}, books_should_move=True) is False
    fresh = {**_STALE, "last_book_change_ts": _STALE["now"] - 10.0}  # 10s since change (< threshold)
    assert _stale_book_reconnect(**fresh, books_should_move=True) is False


# ── build_game_windows: the unknown-flag safety (OR-aggregation; empty-vs-all-unparseable) ─────

def test_windows_mixed_batch_sets_unknown_via_OR():
    # Several parseable + ONE unparseable (mid-list) → unknown MUST be True (OR across ALL pairs, not
    # first/last-wins). Guards: an unreadable game that's actually live slipping through as unknown=False.
    pairs = [_pair("2026-06-24T22:00:00Z", "2026-06-24T23:59:00Z"),
             _pair("2026-06-24T20:00:00Z", "2026-06-24T23:59:00Z"),
             _pair("not-a-date", "2026-06-24T23:59:00Z")]
    windows, unknown = build_game_windows(pairs)
    assert unknown is True            # the unreadable game forces ARMED
    assert len(windows) == 2          # the two readable games still get windows


def test_windows_all_unparseable_is_armed_not_suppressed():
    # The DANGEROUS empty case: empty windows because ALL pairs failed to parse → unknown=True → ARMED.
    windows, unknown = build_game_windows([_pair("x", "y"), _pair(None, None)])
    assert windows == [] and unknown is True


def test_windows_genuinely_empty_is_suppressed():
    # The BENIGN empty case: no markets at all → ([], False) → the watchdog may rest.
    assert build_game_windows([]) == ([], False)


def test_windows_err_long_past_endtime():
    # Window end = endTime + POST_BUFFER, so a game running past endTime stays in-window (never blind).
    start, end = "2026-06-24T22:00:00Z", "2026-06-24T23:59:00Z"
    windows, _ = build_game_windows([_pair(start, end)])
    _s, e = windows[0]
    assert e == parse_iso(end).timestamp() + _WINDOW_POST_BUFFER_S
    assert parse_iso(end).timestamp() + 3 * 3600 <= e          # 3h overrun still inside (POST_BUFFER=4h)


def test_windows_missing_end_uses_generous_fallback():
    # endTime absent → end = start + MISSING_END (then +POST_BUFFER) — a generous window, never None.
    start = "2026-06-24T22:00:00Z"
    windows, _ = build_game_windows([_pair(start, None)])
    _s, e = windows[0]
    assert e == parse_iso(start).timestamp() + _WINDOW_MISSING_END_S + _WINDOW_POST_BUFFER_S


# ── _books_should_move: err toward watching + the WC=1 stale-listing mechanism ─────────────────

def _feed(windows, unknown=False):
    f = PolyUSOrderBookCache(sdk=None)
    f.set_game_windows(windows, unknown)
    return f


def test_books_should_move_startup_armed():
    assert PolyUSOrderBookCache(sdk=None)._books_should_move(10_000.0) is True   # None → uncertain → watch


def test_books_should_move_unknown_armed():
    assert _feed([], unknown=True)._books_should_move(10_000.0) is True          # unreadable timing → watch


def test_books_should_move_no_games_suppressed():
    assert _feed([], unknown=False)._books_should_move(10_000.0) is False        # confidently no games → skip


def test_books_should_move_boundary_inclusive():
    f = _feed([(1000.0, 2000.0)])
    assert f._books_should_move(1500.0) is True    # inside
    assert f._books_should_move(1000.0) is True    # exactly start (inclusive)
    assert f._books_should_move(2000.0) is True    # exactly end (inclusive)
    assert f._books_should_move(2000.1) is False   # just past → suppressed


def test_wc1_stale_listing_suppressed_mechanism():
    # The actual fix on the real churn market: fwc-aut-jor-2026-06-17 (window ends 06-17) during the
    # 2026-06-24 09:16 churn → a week past the buffered window → not in-window → watchdog SKIPS.
    f = _feed(*build_game_windows([_pair("2026-06-17T04:00:00Z", "2026-06-17T23:59:00Z")]))
    assert f._books_should_move(parse_iso("2026-06-24T09:16:00Z").timestamp()) is False  # churn → suppressed
    assert f._books_should_move(parse_iso("2026-06-17T12:00:00Z").timestamp()) is True   # during game → armed
