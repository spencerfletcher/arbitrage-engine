"""safety.cumulative_realized_loss / is_exec_cost_cap_hit — the CUMULATIVE realized-loss
cap (replaced the old rolling in-memory window). Sums realized LOSSES from
logs/execution_pnl.csv: execution_cost (flatten/strand) + realized_settled losses
(void/divergence). Never marked_unsettled. Monotonic → no auto-resume."""
import csv

import bot.core.safety as safety
from bot.core import config


def _write_pnl(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "bucket", "kind", "event",
                    "kalshi_ticker", "qty", "amount", "detail"])
        for r in rows:
            w.writerow(r)


def _row(bucket, amount, kind="x"):
    return ["t", bucket, kind, "E", "KX", "1", f"{amount}", ""]


def test_sums_execution_cost_rows(tmp_path, monkeypatch):
    p = tmp_path / "pnl.csv"
    _write_pnl(p, [_row("execution_cost", 3.0), _row("execution_cost", 4.0)])
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(p))
    assert safety.cumulative_realized_loss() == 7.0


def test_counts_settled_void_loss_not_settled_win(tmp_path, monkeypatch):
    # realized_settled is signed P&L: a void/divergence is negative (loss) → counts its
    # magnitude; a settled WIN is positive → contributes 0 to the loss total.
    p = tmp_path / "pnl.csv"
    _write_pnl(p, [_row("realized_settled", -2.5, "void"),
                   _row("realized_settled", 5.0, "settled_win")])
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(p))
    assert safety.cumulative_realized_loss() == 2.5


def test_marked_unsettled_never_counts(tmp_path, monkeypatch):
    # A run cannot fund (or shrink) the loss cap on booked, unsettled edge.
    p = tmp_path / "pnl.csv"
    _write_pnl(p, [_row("marked_unsettled", 100.0), _row("execution_cost", 1.0)])
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(p))
    assert safety.cumulative_realized_loss() == 1.0


def test_cap_fires_on_simulated_void_loss(tmp_path, monkeypatch):
    # THE required test: a settled void loss pushes cumulative realized loss over budget.
    p = tmp_path / "pnl.csv"
    _write_pnl(p, [_row("execution_cost", 1.0), _row("realized_settled", -5.0, "void")])
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(p))
    monkeypatch.setattr(config, "KALSHI_EXEC_COST_BUDGET", 5.0)
    assert safety.cumulative_realized_loss() == 6.0
    assert safety.is_exec_cost_cap_hit() is True


def test_cap_disabled_when_budget_zero(tmp_path, monkeypatch):
    p = tmp_path / "pnl.csv"
    _write_pnl(p, [_row("execution_cost", 999.0)])
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(p))
    monkeypatch.setattr(config, "KALSHI_EXEC_COST_BUDGET", 0.0)
    assert safety.is_exec_cost_cap_hit() is False


def test_missing_file_is_zero_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(tmp_path / "nope.csv"))
    assert safety.cumulative_realized_loss() == 0.0
    monkeypatch.setattr(config, "KALSHI_EXEC_COST_BUDGET", 5.0)
    assert safety.is_exec_cost_cap_hit() is False
