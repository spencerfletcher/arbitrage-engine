"""
tests/test_safety.py
────────────────────
Unit tests for the kill-switch + daily loss cap helpers. Uses tmp_path
fixture to isolate trades.log and pause.json file paths from the real bot.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.core import config, safety


# ── daily loss cap → tests/test_daily_loss_cap.py ────────────────────────────
# compute_daily_pnl and its tests were REMOVED 2026-07-15: the function could not fire.
# It summed `guaranteed_profit` from trades.log — `shares × edge`, > 0 BY CONSTRUCTION for anything
# that fires — so the sum was always >= 0 and `pnl < -limit` was unreachable for any limit > 0.
#
# The tests hid that, and the way they hid it is worth remembering: they INJECTED
# `{"guaranteed_profit": -0.75}` and stubbed compute_daily_pnl negative — values production never
# produces. The fixture defined a world where the cap worked. Same shape as the V1 FOK-kill mock
# that "proved" the flatten against a venue that no longer existed.
#
# The cap is now re-based on execution_pnl.csv (real realized costs) and pinned in
# tests/test_daily_loss_cap.py against data the bot actually writes.

# ── is_paused ───────────────────────────────────────────────────────────────

def test_is_paused_file_exists(tmp_path, monkeypatch):
    pause_path = tmp_path / "pause.json"
    pause_path.write_text("{}")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(pause_path))
    assert safety.is_paused() is True


def test_is_paused_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "does-not-exist.json"))
    assert safety.is_paused() is False


def test_is_paused_empty_path(monkeypatch):
    """Empty KILL_SWITCH_FILE means feature disabled."""
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", "")
    assert safety.is_paused() is False
