"""Tests for PositionTracker.clear_stranded (resume path) + the strand-invariant
chokepoint (`_delete`): a stranded position is NEVER auto-removed, so the global pause
cannot self-heal. Pinned on the invariant through the public surfaces (reload TTL,
`remove_position`), not on the TTL comprehension itself."""
import time

import pytest

import bot.core.positions as positions
from bot.core.positions import PositionTracker

_8_DAYS = 8 * 24 * 3600   # past the 7-day _load TTL


def test_clear_stranded(tmp_path, monkeypatch):
    # Point state at a temp file so we never touch the real bot_state.json.
    monkeypatch.setattr(positions, "_STATE_FILE", str(tmp_path / "state.json"))
    t = PositionTracker()
    t.add_stranded("ev1", "Game 1", "TOKEN-A", 11.0, 9.46)
    assert t.has_stranded() is True
    assert t.clear_stranded() == 1          # cleared the one strand (sanctioned path)
    assert t.has_stranded() is False        # global pause lifts
    assert t.clear_stranded() == 0          # idempotent — nothing left


def test_stranded_survives_age_ttl_on_reload(tmp_path, monkeypatch):
    """The invariant, through the load path: a stranded leg older than the 7-day TTL must
    SURVIVE a restart — `_load` must not silently lift the global pause. (This is the T1-1
    breach: before the chokepoint the age-TTL deleted it with no strand guard.)"""
    monkeypatch.setattr(positions, "_STATE_FILE", str(tmp_path / "state.json"))
    t = PositionTracker()
    t.add_stranded("ev1", "Game 1", "TOKEN-A", 11.0, 9.46)
    t._positions["TOKEN-A"].entry_time = time.time() - _8_DAYS   # age it past the TTL
    t._save()

    reloaded = PositionTracker()            # triggers _load → runs the age-TTL
    assert "TOKEN-A" in reloaded._positions  # stranded position survived the TTL
    assert reloaded.has_stranded() is True   # → global pause still HELD


def test_remove_position_refuses_stranded(tmp_path, monkeypatch):
    """The chokepoint on the latent path: `remove_position` cannot drop a strand."""
    monkeypatch.setattr(positions, "_STATE_FILE", str(tmp_path / "state.json"))
    t = PositionTracker()
    t.add_stranded("ev1", "Game 1", "TOKEN-A", 11.0, 9.46)
    t.remove_position("TOKEN-A")             # unsanctioned removal → refused
    assert "TOKEN-A" in t._positions
    assert t.has_stranded() is True          # pause held


def test_non_stranded_position_still_ages_out(tmp_path, monkeypatch):
    """The TTL still does its real job: a NORMAL (non-stranded) position older than 7 days
    IS cleared on reload — the fix exempts strands, it does not disable the TTL wholesale."""
    monkeypatch.setattr(positions, "_STATE_FILE", str(tmp_path / "state.json"))
    t = PositionTracker()
    t.add_position("ev1", "Game 1", "TOK-A", 5.0, 10.0, "TOK-B", 5.0, 10.0)
    for tid in ("TOK-A", "TOK-B"):
        t._positions[tid].entry_time = time.time() - _8_DAYS
    t._save()

    reloaded = PositionTracker()
    assert reloaded.list_positions() == []   # both aged out, as designed
    assert reloaded.has_stranded() is False


# ── the write chokepoint ──────────────────────────────────────────────────────────────────────
# `_delete` calls itself "the ONLY place a position leaves `_positions`" and refuses to drop a
# strand. That was false: a dict assignment is not a delete, and both writers used one. The
# existing tests above pin the invariant through `remove_position` and the TTL — the two paths
# that route through `_delete`. The paths that bypassed it had no coverage at all.

def test_add_position_cannot_clear_a_strand_or_lift_the_global_pause(tmp_path, monkeypatch):
    """The one that mattered: `has_stranded()` flipped True→False with no log, and it persisted,
    so a restart did not recover it and `_strand_control_loop` reported all-clear forever.

    Reachable: settlement-equivalent twin tickers share one poly_token and travel in lockstep
    (identical edge, same instant, different cooldown keys), so twin A stranding it and twin B
    hedging it is one batch apart.
    """
    monkeypatch.setattr(positions, "_STATE_FILE", str(tmp_path / "state.json"))
    t = positions.PositionTracker()
    t.add_stranded("ev1", "Twins vs Sox", "POLY-A", 4, 2.05)
    assert t.has_stranded() is True

    t.add_position("ev1", "Twins vs Sox", "POLY-A", 3.94, 8, "KX-A", 3.94, 8)
    assert t.has_stranded() is True, "a later hedge must NEVER clear a strand"

    held = {p.token_id: p for p in t.list_positions()}
    assert held["POLY-A"].is_stranded is True
    assert held["POLY-A"].shares == 12, "we hold BOTH: the 4 stranded and the 8 just bought"

    # ...and it survives a restart, because it was never lost in the first place.
    assert positions.PositionTracker().has_stranded() is True


def test_add_stranded_does_not_erase_the_hedge_on_the_same_token(tmp_path, monkeypatch):
    """`_record_hedge` runs BEFORE `_unwind_poly_excess` and both key on opp.poly_token, so a
    partial Kalshi fill plus a failed flatten erased the hedged shares. The excess is ON TOP of
    what we hedged, not instead of it — the suite missed this because the partial-kfill test uses
    a SUCCESSFUL sell_back and the failed-flatten test uses kfill=0 (so no hedge is booked)."""
    monkeypatch.setattr(positions, "_STATE_FILE", str(tmp_path / "state.json"))
    t = positions.PositionTracker()
    t.add_position("ev1", "A vs B", "POLY-A", 2.70, 6, "KX-AB-A", 2.70, 6)   # hedged 6
    t.add_stranded("ev1", "A vs B", "POLY-A", 4, 2.05)                        # 4 wouldn't sell

    held = {p.token_id: p for p in t.list_positions()}
    assert held["POLY-A"].shares == 10, "reality is 10 Poly: 6 hedged + 4 unsold"
    assert held["POLY-A"].is_stranded is True
    assert held["POLY-A"].cost_basis == pytest.approx(4.75)
    assert t.total_exposure() == pytest.approx(7.45), \
        "the 6 hedged shares must not vanish from the exposure cap"


def test_upsert_keeps_the_first_entry_time_so_the_ttl_measures_from_open(tmp_path, monkeypatch):
    """The age-TTL exists to clear forgotten positions. If a later write reset entry_time, adding
    to a position would keep rejuvenating it."""
    monkeypatch.setattr(positions, "_STATE_FILE", str(tmp_path / "state.json"))
    t = positions.PositionTracker()
    t.add_position("ev1", "A vs B", "POLY-A", 1.0, 2, "KX-A", 1.0, 2)
    first = {p.token_id: p.entry_time for p in t.list_positions()}["POLY-A"]
    time.sleep(0.01)
    t.add_position("ev1", "A vs B", "POLY-A", 1.0, 2, "KX-A", 1.0, 2)
    again = {p.token_id: p.entry_time for p in t.list_positions()}["POLY-A"]
    assert again == first
