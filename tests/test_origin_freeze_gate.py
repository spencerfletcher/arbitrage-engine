"""Tests for the G1 origin-freeze fire-gate — the money-path change, so the EXACT reject
behavior is pinned (money-path-review #7), not just "doesn't crash".

Two units, together covering the gate end to end:
  • _origin_freeze_reject — the pure decision: cross-market frozen burst → REJECT; a lone
    stale-but-live illiquid book → NOT rejected (the §5 stable-deep-book false-positive the gate
    must avoid); a missing/unparseable transactTime → NOT rejected (fail-safe, no manufactured fire).
  • PolyUSOrderBookCache.count_stale_books — the cross-market discriminator: an origin freeze
    stalls MANY books at once (high count); a single illiquid book stale while neighbors tick does
    not; a book with no transactTime is never counted; the firing book itself is excluded.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.runner.kalshi_arb import _origin_freeze_reject
from bot.runner.common import in_window_slugs
from bot.poly_us.feed import PolyUSOrderBookCache


_MAX = 30.0   # mirrors config.FROZEN_BOOK_AGE_S default
_MIN = 2      # mirrors config.ORIGIN_FREEZE_MIN_PEERS default


def _iso(epoch: float) -> str:
    """A Poly book transactTime (ISO-8601, trailing Z) for an absolute epoch second."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ── _origin_freeze_reject: the pure decision ────────────────────────────────────────────────
def test_cross_market_frozen_burst_is_rejected():
    # this book stale AND ≥min peers simultaneously stale → origin freeze → REJECT
    assert _origin_freeze_reject(book_age=200.0, peers_stale=3, max_age=_MAX, min_peers=_MIN) is True
    assert _origin_freeze_reject(book_age=200.0, peers_stale=2, max_age=_MAX, min_peers=_MIN) is True


def test_lone_stale_book_is_not_rejected():
    # this book stale but peers below threshold (a single illiquid book that just didn't tick) →
    # NOT rejected — preserve the real illiquid-but-live edge tail (the §5 false-positive guard)
    assert _origin_freeze_reject(book_age=200.0, peers_stale=1, max_age=_MAX, min_peers=_MIN) is False
    assert _origin_freeze_reject(book_age=200.0, peers_stale=0, max_age=_MAX, min_peers=_MIN) is False


def test_fresh_book_is_not_rejected_regardless_of_peers():
    # this book is fresh → not suspect → never rejected, even if the whole slate is stale
    assert _origin_freeze_reject(book_age=5.0, peers_stale=9, max_age=_MAX, min_peers=_MIN) is False
    assert _origin_freeze_reject(book_age=_MAX, peers_stale=9, max_age=_MAX, min_peers=_MIN) is False  # boundary: ==max not stale


def test_missing_transact_time_is_fail_safe():
    # None age (no/unparseable transactTime) → NOT suspect → never rejected: the gate cannot
    # manufacture a fire OR a false reject from missing freshness data
    assert _origin_freeze_reject(book_age=None, peers_stale=9, max_age=_MAX, min_peers=_MIN) is False


# ── count_stale_books: the cross-market discriminator ───────────────────────────────────────
def _feed_with(times: dict[str, str]) -> PolyUSOrderBookCache:
    f = PolyUSOrderBookCache(sdk=None)
    f._transact_times = dict(times)
    return f


def test_count_stale_books_counts_simultaneous_stale_peers():
    now = 1_800_000_000.0
    # the firing market + 3 neighbors all frozen ~200s (an origin freeze); 1 neighbor fresh
    f = _feed_with({
        "fired":  _iso(now - 200),
        "peerA":  _iso(now - 201),
        "peerB":  _iso(now - 250),
        "peerC":  _iso(now - 373),
        "peerFresh": _iso(now - 2),
    })
    # exclude the firing market; its own staleness is the Stage-1 flag, not a peer
    n = f.count_stale_books(now, _MAX, exclude_token="fired")
    assert n == 3   # peerA/B/C stale, peerFresh not, fired excluded


def test_count_stale_books_single_illiquid_book_has_no_stale_peers():
    now = 1_800_000_000.0
    # only the firing book is stale; every neighbor is ticking fresh → cross-market signal is 0
    f = _feed_with({
        "fired":  _iso(now - 200),
        "peerA":  _iso(now - 1),
        "peerB":  _iso(now - 3),
        "peerC":  _iso(now - 0.5),
    })
    assert f.count_stale_books(now, _MAX, exclude_token="fired") == 0


def test_count_stale_books_ignores_missing_transact_time():
    now = 1_800_000_000.0
    f = _feed_with({"peerA": _iso(now - 200), "peerB": "", "peerC": "not-a-timestamp"})
    # only peerA is a CONFIRMABLE stale book; blank/garbage are not counted (can't confirm → no over-reject)
    assert f.count_stale_books(now, _MAX) == 1


def test_count_stale_books_empty_when_no_transact_times_captured():
    # the inert/fail-safe case: WS never delivered a transactTime → count is 0 → the gate never fires
    f = _feed_with({})
    assert f.count_stale_books(1_800_000_000.0, _MAX, exclude_token="fired") == 0


def test_count_stale_books_excludes_out_of_window_peers():
    now = 1_800_000_000.0
    f = _feed_with({
        "fired":   _iso(now - 200),
        "peerLive": _iso(now - 200),   # in-window AND stale → a real freeze peer
        "peerPre":  _iso(now - 200),   # stale but NOT in the live-game window → must be excluded
    })
    # only peerLive is in the live window (fired excluded as the firing market)
    n = f.count_stale_books(now, _MAX, exclude_token="fired", in_window={"fired", "peerLive"})
    assert n == 1   # peerPre's staleness is legitimately pre-game, not freeze evidence


# ── the quiet-period false-positive (the reason the peer-scoping fix exists) ─────────────────
def test_quiet_period_pregame_peers_do_not_trigger_reject():
    """Fire on a LONE illiquid in-window market (its own book >30s stale → Stage 1 trips) during a
    quiet period with ≥2 stale PRE-GAME markets in the discovery slate. The pre-game peers are
    out-of-window → excluded from the peer count → Stage 2 does NOT trip → NOT rejected. This is the
    §5 illiquid-but-live tail the gate must preserve; before the scoping fix the pre-game peers would
    have falsely confirmed an origin freeze and rejected the real edge."""
    now = 1_800_000_000.0
    f = _feed_with({
        "fired":  _iso(now - 200),   # the lone illiquid in-window market we're firing on (stale book)
        "preA":   _iso(now - 5000),  # an upcoming game, hours from kickoff → legitimately quiet/stale
        "preB":   _iso(now - 9000),  # another pre-game market, also legitimately stale
    })
    # only the firing market is in the live window; the two pre-game markets are NOT
    peers = f.count_stale_books(now, _MAX, exclude_token="fired", in_window={"fired"})
    assert peers == 0                                   # pre-game peers excluded → no freeze confirmation
    assert _origin_freeze_reject(200.0, peers, _MAX, _MIN) is False   # → NOT rejected (edge preserved)


def test_real_freeze_in_window_peers_trigger_reject():
    """The genuine origin freeze still fires: ≥2 OTHER in-window (live-game) markets stale at the same
    time → Stage 2 confirms → REJECT. The scoping fix narrows the peer set, it does not disarm the gate."""
    now = 1_800_000_000.0
    f = _feed_with({
        "fired":     _iso(now - 200),
        "liveA":     _iso(now - 201),   # in-window live game, frozen
        "liveB":     _iso(now - 373),   # in-window live game, frozen
        "preGame":   _iso(now - 9000),  # out-of-window, must not be needed to reach the threshold
    })
    peers = f.count_stale_books(now, _MAX, exclude_token="fired",
                                in_window={"fired", "liveA", "liveB"})
    assert peers == 2                                   # the two in-window frozen peers (preGame excluded)
    assert _origin_freeze_reject(200.0, peers, _MAX, _MIN) is True    # → REJECT (real freeze caught)


# ── in_window_slugs: the fire-gate peer scope (fail toward firing) ──────────────────────────
def _pair(token_a, token_b, start_off, end_off, now):
    """A minimal MarketPair-like object with ISO start/end relative to `now` (offsets in seconds)."""
    return SimpleNamespace(
        token_yes_a=token_a, token_yes_b=token_b,
        start_date=(_iso(now + start_off) if start_off is not None else None),
        end_date=(_iso(now + end_off) if end_off is not None else None),
    )


def test_in_window_slugs_includes_live_excludes_pregame_and_finished():
    now = 1_800_000_000.0
    pairs = [
        _pair("liveA", "liveB", -3600, +3600, now),    # in progress → included
        _pair("preA",  "preB",  +7200, +10800, now),   # kickoff 2h away → now < start → excluded
        _pair("finA",  "finB",  -10800, -7200, now),   # ended 2h ago → now > end → excluded
    ]
    assert in_window_slugs(pairs, now) == {"liveA", "liveB"}


def test_in_window_slugs_excludes_unreadable_start_fail_toward_firing():
    now = 1_800_000_000.0
    pairs = [_pair("liveA", "liveB", None, +3600, now)]   # unreadable start → excluded (not assumed live)
    assert in_window_slugs(pairs, now) == set()


def test_in_window_slugs_missing_end_uses_tight_fallback():
    now = 1_800_000_000.0
    # start 1h ago, no endTime → fallback end = start + 4h = now+3h → still live → included
    assert in_window_slugs([_pair("a", "b", -3600, None, now)], now) == {"a", "b"}
    # start 5h ago, no endTime → fallback end = start + 4h = now-1h → finished → excluded
    # (tight fire-gate fallback, NOT the watchdog's 8h — a long-ago game is not a freeze peer)
    assert in_window_slugs([_pair("c", "d", -5 * 3600, None, now)], now) == set()


def test_in_window_slugs_strips_short_token_to_bare_slug():
    now = 1_800_000_000.0
    # token_yes_b is the ::short complement; the peer set must be BARE slugs (as _transact_times keys)
    assert in_window_slugs([_pair("aec-x", "aec-x::short", -60, +3600, now)], now) == {"aec-x"}


# ── capture + prune on the real feed paths ──────────────────────────────────────────────────
def test_on_market_data_captures_transact_time():
    f = PolyUSOrderBookCache(sdk=None)
    tt = _iso(1_800_000_000.0)
    f._on_market_data({"marketData": {
        "marketSlug": "slugA", "transactTime": tt,
        "offers": [{"px": {"value": "0.40"}, "qty": "100"}],
    }})
    assert f._transact_times["slugA"] == tt


def test_on_market_data_keeps_last_known_when_frame_omits_transact_time():
    f = PolyUSOrderBookCache(sdk=None)
    tt = _iso(1_800_000_000.0)
    f._on_market_data({"marketData": {"marketSlug": "slugA", "transactTime": tt,
                                      "offers": [{"px": {"value": "0.40"}, "qty": "100"}]}})
    # a later frame without transactTime must NOT wipe the last-known stamp (so its age keeps growing)
    f._on_market_data({"marketData": {"marketSlug": "slugA",
                                      "offers": [{"px": {"value": "0.41"}, "qty": "50"}]}})
    assert f._transact_times["slugA"] == tt


def test_retain_slugs_prunes_transact_time():
    f = PolyUSOrderBookCache(sdk=None)
    f._prices["slugA"] = (0.4, 100.0, 0.0, 0.0, 0.0)
    f._transact_times["slugA"] = _iso(1_800_000_000.0)
    f.retain_slugs(set())   # absent cycle 1 — under debounce, kept
    assert "slugA" in f._transact_times
    f.retain_slugs(set())   # absent cycle 2 — pruned from both _prices and _transact_times
    assert "slugA" not in f._transact_times and "slugA" not in f._prices
