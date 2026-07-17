"""Tests for BotRunner._trading_halted — the kill-switch / daily-loss-cap gate
that fronts all live order placement. Verifies it reflects the safety helpers
and throttles re-evaluation (the loss-cap check reads trades.log, so it must not
run on every 0.25s execution tick)."""
import bot.runner.runner as runner_mod
from bot.runner import BotRunner


def _runner(monkeypatch, *, paused=False, loss=False, cost_cap=False):
    monkeypatch.setattr(runner_mod, "is_paused", lambda: paused)
    monkeypatch.setattr(runner_mod, "is_daily_loss_cap_hit", lambda: loss)
    # The cumulative realized-loss cap is its own helper now (was a rolling-window method).
    monkeypatch.setattr(runner_mod, "is_exec_cost_cap_hit", lambda: cost_cap)
    r = BotRunner.__new__(BotRunner)
    r._last_halt_check = 0.0
    r._halted = False
    return r


def test_halted_when_exec_cost_cap_hit(monkeypatch):
    # _trading_halted reflects the cumulative realized-loss cap (cap logic tested in
    # test_exec_cost_cap.py; here we verify the gate is wired to it).
    r = _runner(monkeypatch, paused=False, loss=False, cost_cap=True)
    assert r._trading_halted() is True


def test_not_halted_when_clear(monkeypatch):
    r = _runner(monkeypatch, paused=False, loss=False, cost_cap=False)
    assert r._trading_halted() is False


def test_halted_when_kill_switch_active(monkeypatch):
    r = _runner(monkeypatch, paused=True, loss=False)
    assert r._trading_halted() is True


def test_halted_when_loss_cap_hit(monkeypatch):
    r = _runner(monkeypatch, paused=False, loss=True)
    assert r._trading_halted() is True


def test_result_cached_between_checks(monkeypatch):
    # First call evaluates True (kill switch on) and stamps the check time.
    r = _runner(monkeypatch, paused=True, loss=False)
    assert r._trading_halted() is True
    # Kill switch cleared, but within the throttle window → cached True.
    monkeypatch.setattr(runner_mod, "is_paused", lambda: False)
    assert r._trading_halted() is True
    # Force the throttle to expire → re-evaluates → now False.
    r._last_halt_check = 0.0
    assert r._trading_halted() is False
