"""Tests for the live realized_settled writer (bot/runner/settlement_scorer.py).

The audit's #1 finding: a void/divergence settlement loss is invisible to the cumulative-loss
cap. The load-bearing tests here are the END-TO-END ones — inject a void / divergence settlement
→ assert a realized_settled loss is written → assert safety.is_exec_cost_cap_hit() flips True.
That is the proof the kill can now see settlement losses.
"""
import csv
import json

import pytest

import bot.core.safety as safety
from bot.runner.settlement_scorer import SettlementScorer

_HEADER = ["timestamp", "bucket", "kind", "event", "kalshi_ticker", "qty", "amount", "detail"]


class _FakePoly:
    def __init__(self, price):
        self._price = price
    async def get_settlement(self, token):
        return self._price


class _FakeKalshi:
    """get_market → {'market': {...}} like the real API. value=None omits settlement_value_dollars."""
    def __init__(self, result, value, status="finalized"):
        self._m = {"result": result, "status": status}
        if value is not None:
            self._m["settlement_value_dollars"] = value
    async def get_market(self, ticker):
        return {"market": dict(self._m)}


def _write_trade(path, **over):
    """One executed cross-arb hedge in trades.log. Defaults: 10 sh, fee-inclusive cost $9.70
    (poly_eff 0.50 + kalshi_eff 0.47), held Kalshi NO."""
    rec = {
        "type": "kalshi-arb", "status": "EXECUTED", "event": "A vs B",
        "poly_token": "slug", "kalshi_ticker": "KX-1", "kalshi_side": "no",
        "shares": 10, "cost": 9.70, "poly_ask_eff": 0.50,
        "timestamp": "2026-06-22T20:00:00.000001+00:00",
    }
    rec.update(over)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _build(tmp_path, poly, kalshi, *, w_void=0.10):
    """A scorer wired to tmp execution_pnl.csv (with header) + tmp trades.log + tmp ledger."""
    pnl_path = tmp_path / "execution_pnl.csv"
    with open(pnl_path, "w", newline="") as f:
        csv.writer(f).writerow(_HEADER)
    writer = csv.writer(open(pnl_path, "a", newline="", buffering=1))
    sc = SettlementScorer(
        poly, kalshi, writer,
        trades_path=str(tmp_path / "trades.log"),
        ledger_path=str(tmp_path / "ledger.txt"),
        w_void=w_void,
    )
    return sc, pnl_path


def _rows(pnl_path):
    with open(pnl_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _arm_cap(monkeypatch, pnl_path, budget):
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(pnl_path))
    monkeypatch.setattr(safety.config, "KALSHI_EXEC_COST_BUDGET", budget)


# ── THE PROOF: void / divergence loss trips the cap ───────────────────────────────────

async def test_void_loss_is_written_and_trips_the_cap(tmp_path, monkeypatch):
    # Poly LFMP 0.30 (non-definite → void), Kalshi result='scalar' fair 0.60 (held NO → 0.40).
    # payout 10*(0.30+0.40)=7.00 − cost 9.70 = -2.70 loss.
    sc, pnl_path = _build(tmp_path, _FakePoly(0.30), _FakeKalshi("scalar", "0.60"))
    _write_trade(tmp_path / "trades.log")
    assert await sc.score_once() == 1
    rows = _rows(pnl_path)
    assert len(rows) == 1
    assert rows[0]["bucket"] == "realized_settled" and rows[0]["kind"] == "void"
    assert float(rows[0]["amount"]) == pytest.approx(-2.70, abs=1e-6)
    # the whole point: this settlement loss is now visible to the kill.
    _arm_cap(monkeypatch, pnl_path, budget=1.0)
    assert safety.is_exec_cost_cap_hit() is True
    _arm_cap(monkeypatch, pnl_path, budget=5.0)     # loss 2.70 < 5 → not yet
    assert safety.is_exec_cost_cap_hit() is False


async def test_divergence_both_lose_trips_the_cap(tmp_path, monkeypatch):
    # Both legs DEFINITE but both lose: Poly 0.0, Kalshi result='no' held 'yes' → 0.0.
    # total payout 0 → DIVERGENCE → full stake loss -9.70 (the max-loss mode).
    sc, pnl_path = _build(tmp_path, _FakePoly(0.0), _FakeKalshi("no", "0.00"))
    _write_trade(tmp_path / "trades.log", kalshi_side="yes")
    assert await sc.score_once() == 1
    row = _rows(pnl_path)[0]
    assert row["kind"] == "divergence" and float(row["amount"]) == pytest.approx(-9.70, abs=1e-6)
    _arm_cap(monkeypatch, pnl_path, budget=5.0)
    assert safety.is_exec_cost_cap_hit() is True


# ── condition 4b: missing Kalshi mark floors to a loss, never zero ────────────────────

async def test_void_fallback_floors_loss_when_kalshi_mark_missing(tmp_path, monkeypatch):
    # Poly definite win 1.0, Kalshi result='scalar' but settlement_value_dollars ABSENT →
    # kalshi mark None → must NOT compute zero; floors to -shares*W_VOID = -10*0.10 = -1.0.
    sc, pnl_path = _build(tmp_path, _FakePoly(1.0), _FakeKalshi("scalar", None), w_void=0.10)
    _write_trade(tmp_path / "trades.log")
    assert await sc.score_once() == 1
    row = _rows(pnl_path)[0]
    assert row["kind"] == "void"
    amt = float(row["amount"])
    assert amt == pytest.approx(-1.0, abs=1e-9) and amt != 0.0   # a loss, never zero
    _arm_cap(monkeypatch, pnl_path, budget=0.5)
    assert safety.is_exec_cost_cap_hit() is True


# ── clean / profit does not trip the cap ──────────────────────────────────────────────

async def test_clean_writes_positive_pnl_and_does_not_trip_cap(tmp_path, monkeypatch):
    # Poly win 1.0, Kalshi result='yes' held 'no' → 0.0. total 1.0 → CLEAN. +0.30 profit.
    sc, pnl_path = _build(tmp_path, _FakePoly(1.0), _FakeKalshi("yes", "1.00"))
    _write_trade(tmp_path / "trades.log")
    assert await sc.score_once() == 1
    row = _rows(pnl_path)[0]
    assert row["kind"] == "clean" and float(row["amount"]) == pytest.approx(0.30, abs=1e-6)
    _arm_cap(monkeypatch, pnl_path, budget=0.01)      # a profit contributes 0 loss
    assert safety.is_exec_cost_cap_hit() is False


# ── pending → not written, retried ────────────────────────────────────────────────────

async def test_pending_not_scored(tmp_path):
    sc, pnl_path = _build(tmp_path, _FakePoly(None), _FakeKalshi("", None, status="active"))
    _write_trade(tmp_path / "trades.log")
    assert await sc.score_once() == 0          # not settled → skip
    assert _rows(pnl_path) == []


# ── idempotency: no double on rerun; crash-failure is double-count, never skip ────────

async def test_idempotent_second_run_writes_nothing(tmp_path):
    sc, pnl_path = _build(tmp_path, _FakePoly(0.30), _FakeKalshi("scalar", "0.60"))
    _write_trade(tmp_path / "trades.log")
    assert await sc.score_once() == 1
    assert await sc.score_once() == 0          # already in ledger
    assert len(_rows(pnl_path)) == 1


async def test_crash_before_ledger_write_rescore_double_counts_never_skips(tmp_path):
    # Score once (row + ledger). Simulate a crash AFTER the row, BEFORE the ledger entry by
    # wiping the ledger, then a fresh scorer (reloads the now-empty ledger). The hedge must be
    # RE-SCORED (double row), proving the fail-direction is double-count, not skip.
    poly, kalshi = _FakePoly(0.30), _FakeKalshi("scalar", "0.60")
    sc, pnl_path = _build(tmp_path, poly, kalshi)
    _write_trade(tmp_path / "trades.log")
    await sc.score_once()
    open(tmp_path / "ledger.txt", "w").close()          # crash lost the ledger append
    sc2 = SettlementScorer(poly, kalshi, csv.writer(open(pnl_path, "a", newline="", buffering=1)),
                           trades_path=str(tmp_path / "trades.log"),
                           ledger_path=str(tmp_path / "ledger.txt"), w_void=0.10)
    assert await sc2.score_once() == 1                  # re-scored
    assert len(_rows(pnl_path)) == 2                    # DOUBLE-counted (safe), not skipped


# ── #4 fix: scores purely from trades.log, no PositionTracker needed ──────────────────

async def test_errored_or_ancient_market_is_pending_skip_not_crash_or_double(tmp_path):
    # First live activation reads a trades.log that may hold old hedges. If a ticker is
    # purged/unreachable, fetch_settlement fails closed to PENDING → skip (retry), never a crash
    # and never a row. The un-ledgered hedge is simply retried — idempotency still prevents doubles.
    class _BoomKalshi:
        async def get_market(self, ticker):
            raise RuntimeError("ancient ticker purged / unreachable")
    sc, pnl_path = _build(tmp_path, _FakePoly(1.0), _BoomKalshi())
    _write_trade(tmp_path / "trades.log")
    assert await sc.score_once() == 0          # graceful skip
    assert _rows(pnl_path) == []
    assert await sc.score_once() == 0          # still skipped, no double


async def test_first_activation_scores_only_unscored_across_runs(tmp_path):
    # Non-empty trades.log on first activation: settled hedge scores once + is ledgered;
    # a second (still-pending) hedge is skipped; rerun scores nothing new.
    sc, pnl_path = _build(tmp_path, _FakePoly(0.0), _FakeKalshi("no", "0.00"))   # divergence
    _write_trade(tmp_path / "trades.log", kalshi_side="yes",
                 timestamp="2026-06-22T20:00:00.000001+00:00")
    assert await sc.score_once() == 1
    # add a brand-new hedge AFTER the first scoring run → next run scores only it (not the old one)
    _write_trade(tmp_path / "trades.log", kalshi_side="yes",
                 timestamp="2026-06-22T21:00:00.000002+00:00", kalshi_ticker="KX-2")
    assert await sc.score_once() == 1          # only the new one
    assert len(_rows(pnl_path)) == 2
    assert await sc.score_once() == 0          # both ledgered now


async def test_scores_from_trades_log_with_no_tracker(tmp_path):
    # The scorer's only source is trades.log — no PositionTracker is constructed or passed here,
    # so a hedge the tracker would have TTL'd away (or never held) still gets settled-scored as
    # long as it's in the permanent trades.log. (Guards audit finding #4.)
    sc, pnl_path = _build(tmp_path, _FakePoly(0.0), _FakeKalshi("no", "0.00"))
    _write_trade(tmp_path / "trades.log", kalshi_side="yes")
    assert await sc.score_once() == 1                   # scored with no tracker in play
    assert _rows(pnl_path)[0]["kind"] == "divergence"
