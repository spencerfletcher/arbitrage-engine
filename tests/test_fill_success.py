"""Fill-success observability: _classify_fill_outcome + _log_fill_success (kalshi_arb).

Pins the four outcomes, the blank-not-zero (measured-vs-absent) discipline, the detection→re-read
latency, and the fail-safe guarantee (it must NEVER raise — it's on the fire path before the gates).
"""
import time
from types import SimpleNamespace

from bot.runner.kalshi_arb import _classify_fill_outcome, KalshiArbMixin

# Header order (must match runner.py's fill_success.csv header) → column index map.
_HEADER = [
    "fs_id", "timestamp", "event", "poly_token", "kalshi_ticker", "kalshi_side",
    "outcome", "target_shares", "edge", "poly_ask_raw", "poly_limit", "live_poly_ask", "poly_slip",
    "poly_fillable", "poly_shares_short", "poly_state", "kalshi_ask", "kalshi_limit",
    "kalshi_fillable", "kalshi_shares_short", "fill_window_ms", "poly_read_latency_ms",
    "rest_transact_age_s", "minutes_since_kickoff",
]
COL = {name: i for i, name in enumerate(_HEADER)}


class _CapWriter:
    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(row)


def _self():
    return SimpleNamespace(_fs_csv=_CapWriter(), _fs_seq=0)


def _opp(shares=10, poly_ask_raw=0.40, detected_ts="auto"):
    return SimpleNamespace(
        event_title="A vs B", poly_token="tok", kalshi_ticker="KXMLBGAME-X-A", kalshi_side="yes",
        shares=shares, edge=0.03, poly_ask_raw=poly_ask_raw, kalshi_ask=0.55, kickoff=None,
        detected_ts=(time.time() - 0.030 if detected_ts == "auto" else detected_ts),
    )


def _call(s, opp, *, poly_limit, kalshi_limit, entry_depth, live_poly_ask, poly_state, poly_levels):
    KalshiArbMixin._log_fill_success(s, opp, poly_limit, kalshi_limit, entry_depth,
                                     live_poly_ask, poly_state, poly_levels, 12.0, None)


def test_classify_fill_outcome_four_outcomes():
    assert _classify_fill_outcome(True, True) == "both_fill"
    assert _classify_fill_outcome(True, False) == "kalshi_moved"   # poly ok, kalshi gone
    assert _classify_fill_outcome(False, True) == "poly_moved"     # kalshi ok, poly gone
    assert _classify_fill_outcome(False, False) == "both_moved"


def test_both_fill_row():
    s = _self()
    _call(s, _opp(shares=10), poly_limit=0.42, kalshi_limit=0.58, entry_depth=50,
          live_poly_ask=0.40, poly_state="MARKET_STATE_OPEN", poly_levels=[(0.40, 100)])
    row = s._fs_csv.rows[0]
    assert row[COL["outcome"]] == "both_fill"
    assert row[COL["live_poly_ask"]] == "0.4000"
    assert row[COL["poly_fillable"]] == "100"
    assert row[COL["kalshi_fillable"]] == "50"
    assert row[COL["poly_shares_short"]] == "0"
    # fill_window_ms ~30ms (detected_ts = now − 0.030) → confirms detection→re-read, NOT a 500ms window
    assert 10.0 <= float(row[COL["fill_window_ms"]]) <= 300.0


def test_poly_moved_live_ask_none_is_blank_not_zero():
    s = _self()
    _call(s, _opp(shares=10), poly_limit=0.42, kalshi_limit=0.58, entry_depth=50,
          live_poly_ask=None, poly_state="MARKET_STATE_OPEN", poly_levels=[])
    row = s._fs_csv.rows[0]
    assert row[COL["outcome"]] == "poly_moved"
    assert row[COL["live_poly_ask"]] == ""        # BLANK (book returned no ask) — measured-absent
    assert row[COL["poly_slip"]] == ""            # blank when live ask absent
    assert row[COL["poly_fillable"]] == "0"       # MEASURED 0 (empty book) — distinct from blank
    assert row[COL["poly_shares_short"]] == "10"


def test_poly_moved_price_above_limit_logs_slip():
    s = _self()
    _call(s, _opp(shares=10, poly_ask_raw=0.40), poly_limit=0.42, kalshi_limit=0.58, entry_depth=50,
          live_poly_ask=0.65, poly_state="MARKET_STATE_OPEN", poly_levels=[(0.65, 100)])
    row = s._fs_csv.rows[0]
    assert row[COL["outcome"]] == "poly_moved"    # 0.65 > limit 0.42 → not fillable
    assert row[COL["poly_slip"]] == "0.2500"      # 0.65 − 0.40
    assert row[COL["poly_fillable"]] == "0"       # nothing at ≤ limit


def test_kalshi_moved_depth_short():
    s = _self()
    _call(s, _opp(shares=20), poly_limit=0.42, kalshi_limit=0.58, entry_depth=5,
          live_poly_ask=0.40, poly_state="MARKET_STATE_OPEN", poly_levels=[(0.40, 100)])
    row = s._fs_csv.rows[0]
    assert row[COL["outcome"]] == "kalshi_moved"
    # 15 = the unfilled SHORTFALL (target 20 − entry_depth 5), NOT 15 shares filled. The Kalshi
    # leg "moved" — entry_depth 5 < 20 — so nothing on this leg hedged; _short is the gap, not a fill.
    assert row[COL["kalshi_shares_short"]] == "15"


def test_both_moved():
    s = _self()
    _call(s, _opp(shares=20), poly_limit=0.42, kalshi_limit=0.58, entry_depth=5,
          live_poly_ask=None, poly_state="MARKET_STATE_SUSPENDED", poly_levels=[])
    assert s._fs_csv.rows[0][COL["outcome"]] == "both_moved"


def test_suspended_state_is_not_fillable():
    s = _self()
    # price + depth fine, but state not OPEN → poly leg not fillable
    _call(s, _opp(shares=10), poly_limit=0.42, kalshi_limit=0.58, entry_depth=50,
          live_poly_ask=0.40, poly_state="MARKET_STATE_SUSPENDED", poly_levels=[(0.40, 100)])
    row = s._fs_csv.rows[0]
    assert row[COL["outcome"]] == "poly_moved"
    assert row[COL["poly_state"]] == "MARKET_STATE_SUSPENDED"   # logged, greppable


def test_detected_ts_none_blank_window():
    s = _self()
    _call(s, _opp(detected_ts=None), poly_limit=0.42, kalshi_limit=0.58, entry_depth=50,
          live_poly_ask=0.40, poly_state="MARKET_STATE_OPEN", poly_levels=[(0.40, 100)])
    assert s._fs_csv.rows[0][COL["fill_window_ms"]] == ""       # blank, not 0, when no detection ts


def test_never_raises_fire_path_safe():
    # No _fs_csv attribute → early return, no crash.
    KalshiArbMixin._log_fill_success(SimpleNamespace(), _opp(), 0.4, 0.5, 10,
                                     0.4, "MARKET_STATE_OPEN", [])
    # Garbage poly_levels → exception swallowed (observability must never break firing).
    s = _self()
    KalshiArbMixin._log_fill_success(s, _opp(), 0.4, 0.5, 10, 0.4, "MARKET_STATE_OPEN", "not-levels")
    # reaching here without an exception is the assertion
