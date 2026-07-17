"""
bot/safety.py
─────────────
Stateless safety checks for the trading loop:

  is_paused()                — kill-switch file present?
  is_daily_loss_cap_hit()    — has today's net P&L breached DAILY_LOSS_LIMIT?
  daily_realized_loss()      — today's REALIZED losses from execution_pnl.csv

Pure functions — no shared state with BotRunner. The cross-arb loop calls
both checks every cycle; the same-platform loop relies on PositionTracker's
own stranded-position pause.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

from bot.core import config
from bot.core.logger import get_logger

log = get_logger(__name__)

_EXEC_PNL_PATH = "logs/execution_pnl.csv"


def daily_realized_loss(day: str | None = None) -> float:
    """REALIZED losses for ONE UTC day from logs/execution_pnl.csv (positive = loss).

    Same buckets and same loss-only rule as cumulative_realized_loss — that function is the
    reference; this one just windows it to a day. Counts `execution_cost` (loss-positive: realized
    flatten/strand) and the LOSS side of `realized_settled` (settled void/divergence). NEVER
    `marked_unsettled` (booked, optimistic, always positive) and never a settled PROFIT — a good
    settlement must not FUND the day's loss budget. Missing/unreadable file → 0.0: a cap is a floor,
    a transient read error must not wedge it.

    ⚠️ This REPLACES compute_daily_pnl, which could not fire. It summed `guaranteed_profit` from
    trades.log — `shares × edge`, > 0 BY CONSTRUCTION for anything that fires (edge >= the 2% floor)
    — while flatten/strand costs go to execution_pnl.csv and never reach trades.log. So the sum was
    always >= 0 and `pnl < -limit` was unreachable for ANY limit > 0, in any market condition. Not a
    mis-measured cap: a cap with no trigger.

    NOT redundant with cumulative_realized_loss: that is a LIFETIME ratchet ("stop after $X of
    losses ever"); this is a rate limiter ("stop after $X today"). Only this catches a day of many
    unwinds early — the Σ flatten 70.60 vs Σ won 4.50 shape. Under a $50 lifetime budget, eight $6
    loss-days bleed without tripping the ratchet.
    """
    target = day or datetime.now(timezone.utc).date().isoformat()
    total = 0.0
    try:
        with open(_EXEC_PNL_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not (row.get("timestamp") or "").startswith(target):
                    continue
                bucket = row.get("bucket", "")
                if bucket not in ("execution_cost", "realized_settled"):
                    continue
                try:
                    amount = float(row.get("amount") or 0.0)
                except (TypeError, ValueError):
                    continue
                total += max(0.0, amount) if bucket == "execution_cost" else max(0.0, -amount)
    except FileNotFoundError:
        return 0.0
    except OSError as exc:
        log.warning(f"Daily realized-loss read error: {exc}")
        return 0.0
    return total


def is_daily_loss_cap_hit() -> bool:
    """True (and log a warning) if today's REALIZED losses exceed DAILY_LOSS_LIMIT. 0 = off."""
    limit = config.DAILY_LOSS_LIMIT
    if limit <= 0:
        return False
    loss = daily_realized_loss()
    if loss > limit:
        log.warning(
            f"🛑 Daily loss cap hit: realized losses today = ${loss:.2f} "
            f"(limit=${limit:.2f}). No new trades until tomorrow UTC."
        )
        return True
    return False



def cumulative_realized_loss() -> float:
    """Sum of REALIZED losses across the whole run (CUMULATIVE, not windowed) from
    logs/execution_pnl.csv:
      • every `execution_cost` row — realized flatten/strand cost (`amount` is loss-positive)
      • the loss side of any `realized_settled` row — settled void/divergence (negative P&L)
    NEVER counts `marked_unsettled`: a run cannot fund a loss cap on booked, unsettled edge
    (which is always positive). Reads line-by-line each ~5s halt check. Returns 0.0 if the
    file is missing/unreadable — the cap is a floor, a transient read error must not wedge it.
    """
    total = 0.0
    try:
        with open(_EXEC_PNL_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                bucket = row.get("bucket", "")
                if bucket not in ("execution_cost", "realized_settled"):
                    continue
                try:
                    amount = float(row.get("amount") or 0.0)
                except (TypeError, ValueError):
                    continue
                # execution_cost amounts are loss-positive; realized_settled is signed P&L
                # (negative = loss). Count only the loss side of each.
                total += max(0.0, amount) if bucket == "execution_cost" else max(0.0, -amount)
    except FileNotFoundError:
        return 0.0
    except OSError as exc:
        log.warning(f"Realized-loss read error: {exc}")
        return 0.0
    return total


def is_exec_cost_cap_hit() -> bool:
    """True (and log) if cumulative realized loss has breached KALSHI_EXEC_COST_BUDGET.

    CUMULATIVE + monotonic → once breached it stays breached: NO auto-resume (unlike a
    rolling window or the UTC-resetting daily cap). Independent of booked/marked edge, so a
    losing run can't keep funding itself. The void/divergence tail is NOT bounded by the
    per-trade 25% flatten cap — a void can lose most of a leg (LFMP) — so this campaign-total
    cap is the real floor. 0 budget = disabled.
    """
    budget = config.KALSHI_EXEC_COST_BUDGET
    if budget <= 0:
        return False
    loss = cumulative_realized_loss()
    if loss > budget:
        log.critical(
            f"🛑 Realized-loss cap breached: ${loss:.2f} cumulative realized loss "
            f"(flatten/strand + settled void/divergence) > ${budget:.2f} budget. "
            f"Halting new trades — no auto-resume."
        )
        return True
    return False


def is_paused() -> bool:
    """True (and log a warning) if the kill-switch file exists.

    Operator creates `pause.json` (or whatever KILL_SWITCH_FILE points to) to
    halt new trade execution without restarting. Remove the file to resume.
    """
    path = config.KILL_SWITCH_FILE
    if path and os.path.exists(path):
        log.warning(
            f"⏸️  Kill switch active — '{path}' exists. Remove it to resume trading."
        )
        return True
    return False
