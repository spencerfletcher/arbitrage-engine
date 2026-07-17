"""Tests for BotRunner._confirm_persistent — the persistence gate that requires an
edge to hold for KALSHI_CONFIRM_SECONDS before it's tradeable (filters goal spikes),
and logs edges that vanished before confirming as 'not_persistent' rejections."""
import time
import types
from unittest.mock import MagicMock

import bot.runner.kalshi_arb as kexec
from bot.runner import BotRunner


def _opp(event="ev1", ticker="KX-AB-A", side="no"):
    return types.SimpleNamespace(
        poly_event_id=event, kalshi_ticker=ticker, kalshi_side=side,
        event_title="A vs B", poly_team="A", poly_token="slug",
        poly_ask_raw=0.40, kalshi_ask=0.55, edge=0.05, shares=5,
    )


def _runner(monkeypatch, confirm_s=1.5):
    monkeypatch.setattr(kexec.config, "KALSHI_CONFIRM_SECONDS", confirm_s)
    r = BotRunner.__new__(BotRunner)
    r._confirm_seen = {}
    r._last_reject_log = {}
    r._reject_csv = MagicMock()
    return r


def test_first_sighting_not_confirmed(monkeypatch):
    r = _runner(monkeypatch)
    # Just appeared → tracked but not yet confirmed → empty.
    assert r._confirm_persistent([_opp()]) == []
    assert len(r._confirm_seen) == 1


def test_confirmed_after_window(monkeypatch):
    r = _runner(monkeypatch, confirm_s=1.5)
    opp = _opp()
    r._confirm_persistent([opp])                       # first sighting
    # Backdate first-seen past the window → next pass confirms. Value is (ts, opp) now.
    r._confirm_seen["ev1:KX-AB-A:no"] = (time.time() - 2.0, opp)
    confirmed = r._confirm_persistent([opp])
    assert len(confirmed) == 1 and confirmed[0] is opp


def test_vanished_before_window_pruned_and_logged(monkeypatch):
    r = _runner(monkeypatch)
    r._confirm_persistent([_opp()])                    # track ev1 (lived ~0s)
    assert "ev1:KX-AB-A:no" in r._confirm_seen
    r._confirm_persistent([])                          # vanished before confirming → prune + log
    assert r._confirm_seen == {}
    r._reject_csv.writerow.assert_called_once()
    assert "not_persistent" in r._reject_csv.writerow.call_args.args[0]


def test_confirmed_then_vanished_not_logged_as_nonpersistent(monkeypatch):
    # An edge that DID clear the window then vanished must NOT be double-counted as a
    # persistence rejection (it already had its shot downstream).
    r = _runner(monkeypatch, confirm_s=0.0)
    opp = _opp()
    r._confirm_persistent([opp])                       # first sighting → stored
    r._confirm_seen["ev1:KX-AB-A:no"] = (time.time() - 1.0, opp)  # lived 1.0s ≥ confirm 0.0
    r._confirm_persistent([])                          # vanished, but had confirmed → no log
    r._reject_csv.writerow.assert_not_called()
