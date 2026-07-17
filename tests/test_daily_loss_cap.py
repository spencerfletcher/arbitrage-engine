"""The daily loss cap must be able to FIRE (S6).

It couldn't. compute_daily_pnl summed `guaranteed_profit` from trades.log — which is
`shares × edge`, > 0 BY CONSTRUCTION for anything that fires (edge >= the 2% floor) — while
flatten/strand costs go to execution_pnl.csv and never reach trades.log at all. So the sum was
always >= 0 and `pnl < -limit` was unreachable for ANY limit > 0, in any market condition. Not a
mis-calibrated cap: a cap with no trigger, tagged LIVE in architecture.md.

It is NOT redundant with the cumulative cap. cumulative_realized_loss is a LIFETIME ratchet
("stop after $X of losses ever"); the daily cap is a rate limiter ("stop after $X today"), which is
what catches a day of many unwinds early — the Σ flatten 70.60 vs Σ won 4.50 shape from the
settlement backtest. With a $50 lifetime budget, eight $6 loss-days bleed without tripping it.

Now reads execution_pnl.csv with the SAME bucket rules as cumulative_realized_loss, windowed to a
UTC day.
"""
import pytest

from bot.core import safety


def _write(tmp_path, monkeypatch, rows):
    p = tmp_path / "execution_pnl.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        f.write("timestamp,bucket,kind,event,ticker,qty,amount,note\n")
        for ts, bucket, amount in rows:
            f.write(f"{ts},{bucket},k,E,T,8,{amount},n\n")
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(p))
    return p


def test_a_day_of_flattens_trips_the_cap(tmp_path, monkeypatch):
    """THE case this exists for, and the one it could never see: many small unwinds in one day.
    20 flattens x $0.30 = $6.00 of real, realized loss."""
    _write(tmp_path, monkeypatch, [(f"2026-07-15T{h:02d}:00:00Z", "execution_cost", "0.30")
                                   for h in range(20)])
    assert safety.daily_realized_loss(day="2026-07-15") == pytest.approx(6.0)


def test_yesterdays_losses_do_not_count_today(tmp_path, monkeypatch):
    """The whole point of a DAILY cap vs the lifetime ratchet — it must window."""
    _write(tmp_path, monkeypatch, [
        ("2026-07-14T23:59:00Z", "execution_cost", "50.00"),   # yesterday
        ("2026-07-15T01:00:00Z", "execution_cost", "2.00"),    # today
    ])
    assert safety.daily_realized_loss(day="2026-07-15") == pytest.approx(2.0)


def test_settled_losses_count_but_settled_profits_do_not(tmp_path, monkeypatch):
    """Same loss-only rule as cumulative_realized_loss: count max(0, -amount) on realized_settled.
    A profitable settlement must not FUND the day's loss budget."""
    _write(tmp_path, monkeypatch, [
        ("2026-07-15T01:00:00Z", "realized_settled", "-3.00"),  # a settled LOSS
        ("2026-07-15T02:00:00Z", "realized_settled", "9.00"),   # a settled PROFIT
    ])
    assert safety.daily_realized_loss(day="2026-07-15") == pytest.approx(3.0)


def test_marked_unsettled_is_never_counted(tmp_path, monkeypatch):
    """marked_unsettled is booked, optimistic, always positive. A cap must never read it —
    the project's un-netted-buckets invariant."""
    _write(tmp_path, monkeypatch, [
        ("2026-07-15T01:00:00Z", "marked_unsettled", "100.00"),
        ("2026-07-15T02:00:00Z", "execution_cost", "1.50"),
    ])
    assert safety.daily_realized_loss(day="2026-07-15") == pytest.approx(1.5)


def test_cap_fires_above_the_limit_and_not_below(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [("2026-07-15T01:00:00Z", "execution_cost", "6.00")])
    monkeypatch.setattr(safety, "daily_realized_loss", lambda day=None: 6.0)
    monkeypatch.setattr(safety.config, "DAILY_LOSS_LIMIT", 5.0)
    assert safety.is_daily_loss_cap_hit() is True
    monkeypatch.setattr(safety.config, "DAILY_LOSS_LIMIT", 10.0)
    assert safety.is_daily_loss_cap_hit() is False


def test_off_by_default_and_a_read_error_never_wedges_it(tmp_path, monkeypatch):
    monkeypatch.setattr(safety.config, "DAILY_LOSS_LIMIT", 0.0)
    assert safety.is_daily_loss_cap_hit() is False          # 0 = off
    monkeypatch.setattr(safety, "_EXEC_PNL_PATH", str(tmp_path / "nope.csv"))
    assert safety.daily_realized_loss(day="2026-07-15") == 0.0   # missing file → 0, not a crash
