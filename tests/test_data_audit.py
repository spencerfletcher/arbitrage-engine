"""Tests for scripts/data_audit.py — the load-bearing discipline, not the printing.

Pinned invariants:
  • status() NEVER emits a read below floor — and never even EVALUATES the read thunk there.
  • section_verdict counts settled∩both_fill at GAME granularity, excluding PENDING and
    frozen-book fills, via the SHARED verdict_counts (so it cannot drift from subsecond §C).
  • data_audit and subsecond_calibration share ONE verdict definition (same function objects) —
    the cross-consumer agreement the pending-as-settled bug taught us to enforce structurally.
"""
import scripts.data_audit as da
import scripts.subsecond_calibration as sub


# ── status(): the N-floor discipline ───────────────────────────────────────────────────────
def test_status_below_floor_is_insufficient_and_never_reads():
    def _boom():
        raise AssertionError("read_fn must NOT be evaluated below floor")
    out = da.status("settled∩both_fill", 6, 30, _boom)   # _boom would raise if called
    assert out == "settled∩both_fill: N=6 floor=30 → INSUFFICIENT, 24 from floor"


def test_status_at_or_above_floor_reads():
    assert da.status("x", 30, 30, lambda: "floor reached") == "x: N=30 floor=30 → floor reached"
    assert da.status("x", 31, 30, lambda: "ok") == "x: N=31 floor=30 → ok"


def test_status_no_floor_is_descriptive():
    assert da.status("phantom", 226, None, lambda: "100%") == "phantom: N=226 → 100%"


# ── section_verdict: game-keyed, settled excludes pending, frozen excluded ──────────────────
def _bf(token, ticker, side="yes", day="2026-06-24", age="0.2"):
    return {"outcome": "both_fill", "poly_token": token, "kalshi_ticker": ticker,
            "kalshi_side": side, "timestamp": f"{day}T12:00:00Z", "edge": "0.03",
            "kalshi_fillable": "100", "fill_window_ms": "50", "rest_transact_age_s": age}


def test_verdict_counts_settled_excludes_pending(capsys):
    da._TABLE.clear()
    fill = [
        _bf("tokA", "KXMLBGAME-A-X"),
        _bf("tokB", "KXMLBGAME-B-Y"),
        _bf("tokC", "KXMLBGAME-C-Z"),
    ]
    settle = {
        ("tokA", "KXMLBGAME-A-X", "yes"): "clean",
        ("tokB", "KXMLBGAME-B-Y", "yes"): "void",       # void IS settled
        ("tokC", "KXMLBGAME-C-Z", "yes"): "pending",    # pending is NOT settled
    }
    v = da.section_verdict(fill, settle)
    assert v["n_both"] == 3              # 3 distinct GAMES
    assert v["n_settled"] == 2          # clean + void games, NOT the pending one
    assert v["n_settled_triples"] == 2
    # the status-table row reflects the floor discipline (below 30 → INSUFFICIENT)
    row = next(r for r in da._TABLE if r[0] == "§1")
    assert row[5] == "INSUFFICIENT, 28 from floor"


def test_verdict_absent_capture_row_is_not_settled(capsys):
    da._TABLE.clear()
    fill = [_bf("tokA", "KXMLBGAME-A-X")]
    v = da.section_verdict(fill, settle={})   # no settlement rows at all
    assert v["n_settled"] == 0 and v["n_both"] == 1


def test_verdict_frozen_both_fill_excluded_and_reported(capsys):
    da._TABLE.clear()
    fill = [
        _bf("tokFresh", "KXMLBGAME-A-X", age="0.2"),       # fresh, settled
        _bf("tokFrozen", "KXMLBGAME-B-Y", age="200.0"),    # frozen (>30s), even if settled
    ]
    settle = {
        ("tokFresh", "KXMLBGAME-A-X", "yes"): "clean",
        ("tokFrozen", "KXMLBGAME-B-Y", "yes"): "clean",
    }
    v = da.section_verdict(fill, settle)
    assert v["n_settled"] == 1          # only the fresh game counts toward the floor
    assert v["n_frozen"] == 1           # the frozen row is tallied, not silently dropped
    assert v["n_both"] == 1             # fresh games only


def test_verdict_same_game_triples_collapse_to_one(capsys):
    # real CLE-CWS shapes: 3 fresh settled triples of ONE game → 1 settled GAME (floor unit)
    da._TABLE.clear()
    fill = [
        _bf("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CLE", "yes"),
        _bf("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CWS", "no"),
        _bf("aec-mlb-cle-cws-2026-06-24",        "KXMLBGAME-26JUN241410CLECWS-CWS", "yes"),
    ]
    settle = {da.__dict__["_triple"](r): "clean" for r in fill}
    v = da.section_verdict(fill, settle)
    assert v["n_settled"] == 1           # 1 game
    assert v["n_settled_triples"] == 3   # 3 correlated triples
    assert v["n_both"] == 1 and v["n_triples"] == 3


# ── cross-consumer: ONE verdict definition shared, cannot drift ─────────────────────────────
def test_consumers_share_one_verdict_definition():
    # the structural guarantee that data_audit and subsecond_calibration cannot diverge on the
    # verdict count — they reference the SAME function objects (the pending-as-settled lesson).
    assert da.verdict_counts is sub.verdict_counts
    assert da._game_key is sub._game_key
    assert da._freshness is sub._freshness
    assert da.settled_triple_keys is sub.settled_triple_keys
