"""Tests for the rejected-edge log (_log_reject): writes a row, throttles per reason."""
import time
from types import SimpleNamespace

from bot.runner.kalshi_arb import KalshiArbMixin, _fillable_from_book


class _FakeWriter:
    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(row)


class _Stub(KalshiArbMixin):
    """Minimal carrier for _log_reject — only the attributes it touches."""
    def __init__(self):
        self._reject_csv = _FakeWriter()
        self._last_reject_log = {}


def _opp(edge=0.05, shares=5):
    return SimpleNamespace(
        poly_event_id="e1", kalshi_ticker="KX-1", kalshi_side="no",
        event_title="A vs B", poly_team="A", poly_token="slug",
        poly_ask_raw=0.40, kalshi_ask=0.55, edge=edge, shares=shares,
    )


def test_log_reject_writes_row():
    s = _Stub()
    s._log_reject(_opp(), "thin_entry_depth", "entry_depth=0 < shares=5")
    assert len(s._reject_csv.rows) == 1
    row = s._reject_csv.rows[0]
    assert "thin_entry_depth" in row and "A vs B" in row and "KX-1" in row


def test_log_reject_throttles_same_reason_but_not_different():
    s = _Stub()
    s._log_reject(_opp(), "thin_entry_depth", "d")
    s._log_reject(_opp(), "thin_entry_depth", "d")   # same key within window → suppressed
    assert len(s._reject_csv.rows) == 1
    s._log_reject(_opp(), "stale_price", "d")        # different reason → new row
    assert len(s._reject_csv.rows) == 2


def test_log_reject_no_feeds_leaves_freshness_blank():
    """A stub with no feeds must still write the row — freshness fields just blank."""
    s = _Stub()
    s._log_reject(_opp(), "below_min_edge", "edge=0.01 < min=0.02")
    row = s._reject_csv.rows[0]
    # 16 columns: ts, event, team, token, p_ask, p_age, p_depth, k_ticker, k_side,
    # k_ask, k_age, k_depth, edge, shares, reason, detail.
    assert len(row) == 16
    assert row[5] == "" and row[6] == "" and row[10] == "" and row[11] == ""


def test_log_reject_populates_freshness_from_feeds():
    """With feeds primed, poly/kalshi age + depth land in the row (self-contained audit).
    kalshi_depth is REST-AUTHORITATIVE: even when the WS feed offers a get_depth value, the
    column carries the REST snapshot (the WS ticker depth is the phantom and is never logged)."""
    s = _Stub()
    s._poly_us = True
    s._poly_feed_adapter = SimpleNamespace(
        get_price=lambda _t: SimpleNamespace(last_updated=time.time() - 4.0, ask_depth=2767.0))
    s.kalshi_feed = SimpleNamespace(
        get_age=lambda _tk: 0.3,
        get_depth=lambda _tk, _side: 159491.0)   # WS ticker depth — MUST be ignored
    # opp: side=no, ask=0.55 → threshold 0.45; REST book sums yes_dollars qty where px >= 0.45 = 150.
    s._rest_depth_cache = {"KX-1": (time.time(),
                                    {"orderbook_fp": {"yes_dollars": [[0.45, 100], [0.50, 50]]}})}
    s._log_reject(_opp(), "below_min_edge", "edge=0.01 < min=0.02")
    row = s._reject_csv.rows[0]
    poly_age, poly_depth, kalshi_age, kalshi_depth = row[5], row[6], row[10], row[11]
    assert float(poly_age) >= 3.5 and poly_depth == "2767"
    assert kalshi_age == "0.3"
    assert kalshi_depth == "150"   # REST snapshot wins; the WS 159491 is NOT logged


# ── _fillable_from_book (REST orderbook → fillable-at-limit) ──────────────────

def test_fillable_from_book_no_side_sums_yes_levels_at_threshold():
    # Buy NO at limit 0.55 → threshold 1-0.55=0.45; sum yes_dollars qty where px >= 0.45.
    book = {"orderbook_fp": {"yes_dollars": [[0.45, 100], [0.50, 50], [0.30, 999]]}}
    assert _fillable_from_book(book, "no", 0.55) == 150     # 0.30 level excluded


def test_fillable_from_book_yes_side_reads_no_levels():
    # Buy YES at limit 0.55 → threshold 0.45; sum no_dollars qty where px >= 0.45.
    book = {"orderbook_fp": {"no_dollars": [[0.60, 30], [0.40, 7]]}}
    assert _fillable_from_book(book, "yes", 0.55) == 30     # 0.40 excluded


def test_fillable_from_book_empty_and_garbage_safe():
    assert _fillable_from_book({}, "no", 0.55) == 0.0
    assert _fillable_from_book({"orderbook_fp": {"yes_dollars": [["x", "y"], [0.9, 5]]}}, "no", 0.5) == 5


# ── reject-log kalshi_depth REST fallback (ticker mode has no WS book) ─────────

def _ticker_mode_stub():
    s = _Stub()
    s._poly_us = True
    s._poly_feed_adapter = SimpleNamespace(get_price=lambda _t: None)
    # get_depth=None mimics ticker mode (no WS orderbook maintained)
    s.kalshi_feed = SimpleNamespace(get_age=lambda _tk: 0.3, get_depth=lambda _tk, _side: None)
    return s


def test_log_reject_kalshi_depth_falls_back_to_rest_cache():
    s = _ticker_mode_stub()
    s._rest_depth_cache = {"KX-1": (time.time(),
                                    {"orderbook_fp": {"yes_dollars": [[0.45, 100], [0.50, 50]]}})}
    s._log_reject(_opp(), "below_min_edge", "d")          # opp: side=no, ask=0.55 → thr 0.45
    assert s._reject_csv.rows[0][11] == "150"            # kalshi_depth from the cached REST book


def test_log_reject_kalshi_depth_stale_cache_logs_value_with_age():
    s = _ticker_mode_stub()
    s._rest_depth_cache = {"KX-1": (time.time() - 60.0,
                                    {"orderbook_fp": {"yes_dollars": [[0.45, 100]]}})}
    s._log_reject(_opp(), "below_min_edge", "d")
    # ≥30s → still report the ACTUAL fillable, tagged with its age (never just blank).
    assert s._reject_csv.rows[0][11] == "100@60s"


def test_log_reject_kalshi_depth_no_snapshot_when_ticker_never_fetched():
    s = _ticker_mode_stub()
    s._rest_depth_cache = {}                              # this ticker never reached the fire path
    s._log_reject(_opp(), "below_min_edge", "d")
    assert s._reject_csv.rows[0][11] == "no_snapshot"


# ── shared _kalshi_rest_depth_str sentinel (used by _log_reject AND the edge_freshness logger) ──
# Pins each of the four branches directly on the helper, so the edge_freshness depth column — which
# has no isolated writer to test — is covered by the same authority. side="no", ask=0.55 → buying NO
# lifts YES bids at px ≥ 1−0.55 = 0.45.

def _depth_book():
    return {"orderbook_fp": {"yes_dollars": [[0.45, 100], [0.50, 50]]}}   # fillable @0.45 = 150


def test_rest_depth_str_fresh_hit_bare_number():
    s = _Stub()
    s._rest_depth_cache = {"KX-1": (1000.0, _depth_book())}
    assert s._kalshi_rest_depth_str("KX-1", "no", 0.55, now=1000.0) == "150"   # age 0 < 30 → bare


def test_rest_depth_str_stale_hit_tagged_with_age():
    s = _Stub()
    s._rest_depth_cache = {"KX-1": (1000.0, {"orderbook_fp": {"yes_dollars": [[0.45, 100]]}})}
    assert s._kalshi_rest_depth_str("KX-1", "no", 0.55, now=1047.0) == "100@47s"   # 47s ≥ 30 → @age


def test_rest_depth_str_age_boundary_30s_is_tagged():
    # Pin which side the exact 30.0s boundary falls on: helper is `age < 30.0 → bare, else tagged`,
    # so age == 30.0 is tagged. Guards a future <→<= flip the 15s/47s cases would miss.
    s = _Stub()
    s._rest_depth_cache = {"KX-1": (1000.0, {"orderbook_fp": {"yes_dollars": [[0.45, 100]]}})}
    assert s._kalshi_rest_depth_str("KX-1", "no", 0.55, now=1030.0) == "100@30s"


def test_rest_depth_str_no_snapshot_on_cache_miss():
    s = _Stub()                                          # no _rest_depth_cache attr at all
    assert s._kalshi_rest_depth_str("KX-1", "no", 0.55, now=1000.0) == "no_snapshot"


def test_rest_depth_str_measured_zero_is_not_no_snapshot():
    # A real empty book (best YES bid 0.30 < 0.45 threshold → 0 fillable) logs "0", NOT "no_snapshot"
    # (measured-empty ≠ never-measured) and NOT a blank — the blank-vs-zero distinction.
    s = _Stub()
    s._rest_depth_cache = {"KX-1": (1000.0, {"orderbook_fp": {"yes_dollars": [[0.30, 100]]}})}
    assert s._kalshi_rest_depth_str("KX-1", "no", 0.55, now=1000.0) == "0"


def test_rest_depth_str_tuple_order_ts_then_book():
    # Pin (timestamp, book) order: age from hit[0], fillable from hit[1]. A swapped cache tuple would
    # feed a float to _fillable_from_book / subtract a dict — this exact-value assert only holds for
    # the correct order, so it guards the silent-garbage failure mode of an index swap.
    s = _Stub()
    s._rest_depth_cache = {"KX-1": (900.0, _depth_book())}
    assert s._kalshi_rest_depth_str("KX-1", "no", 0.55, now=915.0) == "150"   # 15s old, fillable 150


def test_direction_key_distinct_per_kalshi_leg():
    """Cooldown keys off _direction_key, so two directions on the SAME event get
    independent cooldowns — one firing doesn't lock out the other."""
    a = SimpleNamespace(poly_event_id="e1", kalshi_ticker="KX-A", kalshi_side="no")
    b = SimpleNamespace(poly_event_id="e1", kalshi_ticker="KX-B", kalshi_side="yes")
    same_event_other_side = SimpleNamespace(poly_event_id="e1", kalshi_ticker="KX-A", kalshi_side="yes")
    assert KalshiArbMixin._direction_key(a) == "e1:KX-A:no"
    assert KalshiArbMixin._direction_key(a) != KalshiArbMixin._direction_key(b)
    assert KalshiArbMixin._direction_key(a) != KalshiArbMixin._direction_key(same_event_other_side)
