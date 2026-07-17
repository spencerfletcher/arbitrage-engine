"""
bot/positions.py
────────────────
Persistent position and cooldown tracker.

Tracks:
  - Open positions (tokens bought that haven't resolved yet)
  - Event cooldowns (events recently traded, survives restarts)

State is persisted to a JSON file so it survives bot restarts.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

from bot.core import config
from bot.core.logger import get_logger

log = get_logger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "bot_state.json")


@dataclass
class OpenPosition:
    """A token position we're currently holding."""
    event_id: str
    event_title: str
    token_id: str
    shares: float
    cost_basis: float   # Total USD paid
    entry_time: float   # Unix timestamp
    is_stranded: bool   # True if this is from a failed unwind


class PositionTracker:
    """
    Tracks open positions and event cooldowns with disk persistence.

    Provides:
      - total_exposure()   → sum of cost_basis across all open positions
      - is_on_cooldown()   → whether an event was traded recently
      - add_position()     → record a new position (from successful arb)
      - add_stranded()     → record a stranded leg that failed to unwind
      - remove_position()  → remove when market resolves or position sold
      - mark_traded()      → set cooldown for an event
    """

    def __init__(self) -> None:
        self._positions: dict[str, OpenPosition] = {}   # token_id → position
        self._cooldowns: dict[str, float] = {}           # event_id → timestamp
        self._load()

    # ── Exposure ──────────────────────────────────────────────────────────

    def total_exposure(self) -> float:
        """Sum of cost basis across all open positions."""
        return sum(p.cost_basis for p in self._positions.values())

    def add_position(
        self, event_id: str, event_title: str,
        token_a: str, cost_a: float, shares_a: float,
        token_b: str, cost_b: float, shares_b: float,
    ) -> None:
        """Record both legs of a successful arb trade.

        Routes through `_upsert`, so it ADDS to any existing holding rather than replacing it and
        can never clear a strand — it used to assign `is_stranded=False` straight into the dict,
        which silently lifted the global pause.
        """
        self._upsert(token_a, event_id=event_id, event_title=event_title,
                     shares=shares_a, cost_basis=cost_a, is_stranded=False)
        self._upsert(token_b, event_id=event_id, event_title=event_title,
                     shares=shares_b, cost_basis=cost_b, is_stranded=False)
        self._save()

    def add_stranded(
        self, event_id: str, event_title: str,
        token_id: str, shares: float, cost_basis: float,
    ) -> None:
        """Record a stranded leg that failed to unwind.

        Routes through `_upsert`, so it ADDS to any hedge already recorded on this token rather
        than erasing it — the excess is *on top of* what we hedged, not instead of it.
        """
        self._upsert(token_id, event_id=event_id, event_title=event_title,
                     shares=shares, cost_basis=cost_basis, is_stranded=True)
        log.warning(
            f"🚨 STRANDED position recorded: {event_title} "
            f"token={token_id[:8]}… shares={shares:.0f} cost=${cost_basis:.2f}"
        )
        self._save()

    def _upsert(self, token_id: str, *, event_id: str, event_title: str,
                shares: float, cost_basis: float, is_stranded: bool) -> None:
        """Single WRITE chokepoint — ADDS to what we hold at `token_id`, never replaces it.

        `_delete` below calls itself "the ONLY place a position leaves `_positions`" and refuses
        to drop a strand. That was false, because **a dict assignment is not a delete**:
        `self._positions[tok] = OpenPosition(...)` removes whatever was there without ever
        consulting `_delete`, and both writers did exactly that. Two consequences, both verified
        by execution 2026-07-16:

        (a) `add_position` wrote `is_stranded=False` over a stranded row → `has_stranded()` flipped
            True→False → **the global pause lifted, silently**, no log, and it persisted to
            bot_state.json so a restart did not recover it. `_strand_control_loop` then reported
            all-clear forever. Reachable: settlement-equivalent twin tickers share one poly_token
            and travel in lockstep (identical edge, same instant, different cooldown keys), so twin
            A stranding it and twin B hedging it is one batch apart.
        (b) `add_stranded` wrote over the hedge record for the same token. In `_exec_poly_first`,
            `_record_hedge` runs BEFORE `_unwind_poly_excess` and both key on `opp.poly_token`, so
            a partial Kalshi fill plus a failed flatten left 6 hedged shares erased by the 4
            stranded ones — 6 real shares invisible to `total_exposure()` and to the strand loop.

        Adding is also just correct: two writes to one token mean we bought more of one
        instrument, and the truth is the sum. Case (b)'s reality is 10 Poly (6 hedged + 4 unsold),
        which is what this now records.

        `is_stranded` is sticky — `prev or new`. A strand may only ever be cleared by
        `clear_stranded`, the operator-gated path, exactly as `_delete` intends. `entry_time`
        keeps the FIRST write's value so the age-TTL measures from when we actually opened.
        """
        prev = self._positions.get(token_id)
        if prev is not None:
            shares += prev.shares
            cost_basis += prev.cost_basis
            is_stranded = prev.is_stranded or is_stranded
            event_id, event_title = prev.event_id, prev.event_title
        self._positions[token_id] = OpenPosition(
            event_id=event_id, event_title=event_title, token_id=token_id,
            shares=shares, cost_basis=cost_basis,
            entry_time=prev.entry_time if prev is not None else time.time(),
            is_stranded=is_stranded,
        )

    def _delete(self, token_id: str, *, sanctioned: bool = False) -> bool:
        """Single removal chokepoint — the ONLY place a position leaves `_positions`.

        Enforces the strand invariant once: a stranded position is NEVER auto-removed.
        Only `clear_stranded` (operator-gated via `logs/resume`) may remove one, and it
        passes `sanctioned=True`. Every other path (age-TTL, `remove_position`) routes
        through here unsanctioned, so it physically cannot drop a strand and silently
        lift the global pause. Returns True iff the position was actually removed.

        ⚠️ "The ONLY place" only became TRUE on 2026-07-16. Until then the two writers assigned
        straight into `_positions`, and an assignment is an implicit delete — so a strand could be
        dropped without this function ever running, and was. Both now route through `_upsert`,
        which adds rather than replaces and keeps `is_stranded` sticky. If you add a third writer,
        route it through `_upsert` too: this docstring is only as true as the writers make it.
        """
        pos = self._positions.get(token_id)
        if pos is None:
            return False
        if pos.is_stranded and not sanctioned:
            # A should-never-happen near-miss: the invariant was about to be violated.
            # Refuse, hold the pause, and shout — do NOT fail silently.
            log.critical(
                f"🚨 REFUSED to auto-remove a STRANDED position — global pause HELD. "
                f"Clear only via logs/resume. '{pos.event_title}' (token {token_id[:8]}…)"
            )
            return False
        del self._positions[token_id]
        return True

    def remove_position(self, token_id: str) -> None:
        """Remove a position (market resolved or sold).

        Routes through `_delete`, so it cannot remove a stranded leg (the strand stays
        until explicitly cleared via `clear_stranded`). Fails toward holding the pause.
        """
        if self._delete(token_id):
            self._save()

    def list_positions(self) -> list[OpenPosition]:
        return list(self._positions.values())

    def has_stranded(self) -> bool:
        """Return True if any open position is marked as stranded."""
        return any(p.is_stranded for p in self._positions.values())

    def clear_stranded(self) -> int:
        """Drop all stranded position records and return how many were cleared.

        The real position is the user's to handle (sell/hold) — this only clears
        the bot's STRANDED flag so it can resume trading (the global pause lifts).
        """
        stranded = [tid for tid, p in self._positions.items() if p.is_stranded]
        for tid in stranded:
            self._delete(tid, sanctioned=True)   # the ONE sanctioned strand-removal path
        if stranded:
            self._save()
        return len(stranded)

    # ── Cooldowns ─────────────────────────────────────────────────────────


    def is_on_cooldown(self, event_id: str) -> bool:
        last = self._cooldowns.get(event_id)
        if last is None:
            return False
        return (time.time() - last) < config.EVENT_COOLDOWN_SECONDS

    def mark_traded(self, event_id: str) -> None:
        self._cooldowns[event_id] = time.time()
        self._save()

    # ── Persistence ───────────────────────────────────────────────────────

    def _save(self) -> None:
        """Write state to disk."""
        try:
            state = {
                "positions": {
                    tid: asdict(pos) for tid, pos in self._positions.items()
                },
                "cooldowns": self._cooldowns,
            }
            tmp_file = _STATE_FILE + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_file, _STATE_FILE)
        except Exception as exc:
            log.error(f"Failed to save state: {exc}")

    def _load(self) -> None:
        """Load state from disk."""
        if not os.path.exists(_STATE_FILE):
            return
        try:
            with open(_STATE_FILE, "r") as f:
                state = json.load(f)
            for tid, pdata in state.get("positions", {}).items():
                self._positions[tid] = OpenPosition(**pdata)
            self._cooldowns = state.get("cooldowns", {})
            # Clean up expired cooldowns
            now = time.time()
            self._cooldowns = {
                eid: ts for eid, ts in self._cooldowns.items()
                if (now - ts) < config.EVENT_COOLDOWN_SECONDS
            }
            # Clean up expired positions (7-day TTL = 604800s). Stranded positions are
            # EXEMPT — `_delete` refuses them (and logs CRITICAL), so the strand global
            # pause survives a 7-day-plus restart instead of self-healing silently.
            ttl_seconds = 7 * 24 * 3600
            expired_tids = [
                tid for tid, p in self._positions.items()
                if (now - p.entry_time) > ttl_seconds
            ]
            for tid in expired_tids:
                title = self._positions[tid].event_title
                if self._delete(tid):   # refuses + logs CRITICAL if stranded; strand stays
                    log.warning(
                        f"🧹 WARNING: Auto-clearing expired position (>7 days). "
                        f"Verify resolution manually: '{title}' (token {tid})"
                    )

            exp = len(self._positions)
            log.info(
                f"📋 Loaded state: {exp} open position(s), "
                f"${self.total_exposure():.0f} exposure"
            )
        except Exception as exc:
            log.error(f"Failed to load state: {exc}")
