"""Tests for the VWAP size-vs-edge curve instrumentation (logging-only, fire path).

The curve answers the one unmeasured question gating scale: the logged `edge` is TOP-OF-BOOK and
decays toward 0 as you walk to the breakeven limit — so where does it actually die, 20 shares or
500? (CLAUDE.md Posture: size is a chosen testing ramp, NOT a depth limit; only 3% of both_fills
are depth-capped. `edge x fillable` is circular — this replaces it with a real walk.)

Three things are load-bearing and pinned here:
  1. Fees come from the CANONICAL fns (cross_arb._effective_share_cost / _kalshi_taker_fee) and are
     charged PER LEVEL — never re-derived, never applied once at the VWAP. Kalshi's fee ceil()s the
     BATCH total then divides by count, so the per-contract fee genuinely varies with size.
  2. At size 1 on a flat book the curve must reproduce the canonical edge EXACTLY — that is the
     anti-drift pin against the fee model.
  3. The instrumentation can NEVER raise into the fire path (it is observability; a bad book must
     degrade to None, never to an exception that skips a trade or crashes the loop).
"""
import pytest

from bot.kalshi.cross_arb import check_kalshi_arb, _effective_share_cost, _kalshi_taker_fee
from bot.runner.kalshi_arb import (
    _SIZE_CURVE_POINTS,
    _fillable_from_book,
    _kalshi_ask_levels,
    _realized_edge_at,
    _size_curve,
    _walk_effective_cost,
)


def test_kalshi_ask_levels_agrees_with_fire_path_convention():
    """Ask-space conversion must mirror _fillable_from_book (buying a side lifts the OPPOSING
    bids: a bid at p is an offer at 1-p). If these disagree the whole curve is inverted."""
    book = {"orderbook_fp": {"yes_dollars": [[0.60, 100], [0.55, 50]],
                             "no_dollars": [[0.30, 77], [0.25, 20]]}}
    lv = _kalshi_ask_levels(book, "yes")
    assert lv[0] == (0.70, 77.0)          # best (cheapest) ask first
    assert lv[1] == (0.75, 20.0)
    # cross-check against the fire-path fn on the same book
    assert sum(q for p, q in lv if p <= 0.75) == pytest.approx(_fillable_from_book(book, "yes", 0.75))
    assert sum(q for p, q in _kalshi_ask_levels(book, "no") if p <= 0.45) == pytest.approx(
        _fillable_from_book(book, "no", 0.45))


def test_walk_is_none_when_ladder_cannot_supply_size():
    assert _walk_effective_cost([(0.5, 10.0)], 25, lambda p, q: p) is None
    assert _walk_effective_cost([], 1, lambda p, q: p) is None


def test_walk_spans_levels_and_weights_by_qty():
    # 10 @ 0.40 + 5 @ 0.50 = 4.0 + 2.5 = 6.5 for 15 contracts
    got = _walk_effective_cost([(0.40, 10.0), (0.50, 90.0)], 15, lambda p, q: p)
    assert got == pytest.approx(6.5)


def test_realized_edge_reproduces_canonical_edge_at_size_1():
    """ANTI-DRIFT PIN: on a flat book at n=1 the curve must equal check_kalshi_arb exactly.
    If someone re-derives the fee model, this fails."""
    for p, k, rate in ((0.45, 0.50, 0.0), (0.30, 0.65, 0.05), (0.50, 0.48, 0.07)):
        poly, kal = [(p, 1000.0)], [(k, 1000.0)]
        assert _realized_edge_at(poly, kal, 1, rate) == pytest.approx(
            check_kalshi_arb(p, k, rate), abs=1e-9)


def test_realized_edge_decays_as_size_grows():
    """The whole point: walking into worse levels must lower the realized per-share edge."""
    poly = [(0.40, 10.0), (0.45, 10.0), (0.49, 980.0)]
    kal = [(0.45, 10.0), (0.48, 10.0), (0.50, 980.0)]
    e10 = _realized_edge_at(poly, kal, 10, 0.0)
    e100 = _realized_edge_at(poly, kal, 100, 0.0)
    assert e10 > e100, "edge must decay as size walks the ladder"


def test_kalshi_fee_is_charged_per_level_not_once_at_the_vwap():
    """Kalshi's fee ceil()s the batch total, so charging it once at the VWAP is NOT the same as
    charging per level. Pin the per-level behaviour explicitly."""
    lv = [(0.40, 10.0), (0.60, 10.0)]
    got = _walk_effective_cost(lv, 20, lambda p, q: p + _kalshi_taker_fee(p, max(1, int(round(q)))))
    expect = ((0.40 + _kalshi_taker_fee(0.40, 10)) * 10.0
              + (0.60 + _kalshi_taker_fee(0.60, 10)) * 10.0)
    assert got == pytest.approx(expect)
    # VWAP here is 0.50 — the naive one-shot fee differs, proving the distinction is real
    naive = (0.50 + _kalshi_taker_fee(0.50, 20)) * 20.0
    assert got != pytest.approx(naive)


def test_poly_fee_uses_canonical_effective_cost():
    lv = [(0.30, 5.0)]
    got = _walk_effective_cost(lv, 5, lambda p, q: _effective_share_cost(p, 0.05))
    assert got == pytest.approx(_effective_share_cost(0.30, 0.05) * 5.0)


def test_size_curve_reports_points_and_profit_maximising_size():
    poly = [(0.40, 50.0), (0.48, 5000.0)]
    kal = [(0.45, 50.0), (0.50, 5000.0)]
    out = _size_curve(poly, kal, 0.0, points=(8, 25, 100))
    assert out is not None
    assert set(out["points"]) == {8, 25, 100}
    assert out["points"][8] > out["points"][100]          # decays
    assert out["best_n"] in (8, 25, 100)
    assert out["best_profit"] == pytest.approx(
        max(n * e for n, e in out["points"].items() if e is not None))


def test_size_curve_marks_unreachable_sizes_none_not_zero():
    """blank/None = 'ladder can't supply it', which is NOT 'zero edge' (log-review trap #2)."""
    out = _size_curve([(0.40, 10.0)], [(0.45, 10.0)], 0.0, points=(8, 1000))
    assert out["points"][8] is not None
    assert out["points"][1000] is None


@pytest.mark.parametrize("bad_book", [
    None, {}, {"orderbook_fp": {}},
    {"orderbook_fp": {"no_dollars": [["x", "y"], None, [0.3], [0.3, "q"]]}},
    {"orderbook_fp": {"no_dollars": "not-a-list"}},
])
def test_never_raises_into_the_fire_path(bad_book):
    """SAFETY: this is observability on the fire path. Garbage must degrade, never raise."""
    lv = _kalshi_ask_levels(bad_book, "yes")
    assert isinstance(lv, list)
    assert _size_curve(None, lv, 0.0) is None
    assert _size_curve([(0.4, 10.0)], lv, 0.0) is None or isinstance(_size_curve([(0.4, 10.0)], lv, 0.0), dict)


def test_size_curve_none_on_garbage_inputs():
    assert _size_curve(None, None, 0.0) is None
    assert _size_curve([], [], 0.0) is None
    assert _size_curve([(0.4, 10.0)], [], 0.0) is None


# ── the fire-path wrapper itself (money-path checklist #7: pin the NEW branch, not just the
#    pure helpers — this is the code that could raise into _log_would_fire and skip a trade) ──

class _Opp:
    poly_ask_raw = 0.45
    poly_ask = 0.45
    kalshi_ticker = "KXMLBGAME-26JUL14AAABBB-AAA"
    kalshi_side = "yes"
    edge = 0.04
    shares = 8
    event_title = "A vs B"


def _runner(buf):
    import csv as _csv
    from bot.runner.kalshi_arb import KalshiArbMixin
    r = KalshiArbMixin.__new__(KalshiArbMixin)
    r._size_curve_csv = _csv.writer(buf)
    return r


def _book(levels):
    return {"orderbook_fp": {"no_dollars": levels}}


def test_log_size_curve_writes_a_joinable_row_with_decay_and_best_n():
    import csv as _csv
    import io
    buf = io.StringIO()
    r = _runner(buf)
    # no bids .50/.45 -> yes asks .50 (60 deep) then .55 (5000 deep)
    r._rest_depth_cache = {_Opp.kalshi_ticker: (0.0, _book([[0.50, 60], [0.45, 5000]]))}
    r._log_size_curve(7, _Opp(), [(0.44, 60.0), (0.47, 5000.0)])
    row = next(_csv.reader(io.StringIO(buf.getvalue().strip())))
    assert row[1] == "7"                       # wf_id — the join key to would_fire.csv
    assert row[2] == "A vs B"
    n_pts = len(_SIZE_CURVE_POINTS)
    assert len(row) == 9 + n_pts + 2           # header contract: 9 meta + N edges + best_n/profit
    edges = row[9:9 + n_pts]
    assert float(edges[0]) > float(edges[3])   # decays with size
    assert row[-2] != ""                       # best_n present


def test_log_size_curve_is_a_noop_without_the_kalshi_book_cache():
    import io
    buf = io.StringIO()
    r = _runner(buf)
    r._rest_depth_cache = {}
    r._log_size_curve(1, _Opp(), [(0.44, 60.0)])
    assert buf.getvalue() == ""


@pytest.mark.parametrize("bad", [
    _book("garbage"), _book([["x", "y"]]), {}, None,
])
def test_log_size_curve_never_raises_on_bad_books(bad):
    import io
    buf = io.StringIO()
    r = _runner(buf)
    r._rest_depth_cache = {_Opp.kalshi_ticker: (0.0, bad)}
    r._log_size_curve(1, _Opp(), [(0.44, 60.0)])   # must not raise


def test_log_size_curve_swallows_an_exploding_opp():
    """SAFETY: it is called from _log_would_fire ON the fire path. If anything inside it can raise,
    it can skip a would_fire log — or worse. Nothing may escape."""
    import io
    buf = io.StringIO()
    r = _runner(buf)
    r._rest_depth_cache = {}

    class Boom:
        poly_ask_raw = 0.45
        poly_ask = 0.45
        kalshi_side = "yes"

        @property
        def kalshi_ticker(self):
            raise RuntimeError("boom")

    r._log_size_curve(1, Boom(), [(0.44, 60.0)])   # must not raise


def test_log_size_curve_survives_a_missing_csv_writer():
    """A runner constructed without the CSV (mocks/tests) must not blow up the fire path."""
    from bot.runner.kalshi_arb import KalshiArbMixin
    r = KalshiArbMixin.__new__(KalshiArbMixin)
    r._rest_depth_cache = {_Opp.kalshi_ticker: (0.0, _book([[0.50, 60]]))}
    r._log_size_curve(1, _Opp(), [(0.44, 60.0)])   # no _size_curve_csv attribute — must not raise
