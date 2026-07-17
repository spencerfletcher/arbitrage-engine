"""
bot/runner/settlement_scorer.py
───────────────────────────────
The LIVE in-process writer of the `realized_settled` P&L bucket — the one component that
lets the cumulative-loss kill (safety.is_exec_cost_cap_hit) see SETTLEMENT losses (void /
divergence), not just flatten/strand execution costs. Without it a void/divergence at
settlement is invisible to the cap (the documented #1 go-live blocker).

Design (each point is a deliberate safety choice):
  • SOURCE = logs/trades.log (the permanent JSONL of executed hedges), NEVER the
    PositionTracker. trades.log carries kalshi_side + the FEE-INCLUSIVE entry `cost`, survives
    restarts, and is not subject to the tracker's 7-day TTL — so a hedge the tracker aged out
    still gets settled-scored. The scorer NEVER reads or mutates position state.
  • P&L from trades.log's fee-inclusive `cost` (poly_eff+kalshi_eff = cost/shares), never the
    tracker's fee-light basis — so losses aren't understated (wrong way for a safety cap).
  • VOID via settlement.realized_pnl_live: actual marked loss when both legs are marked, else a
    conservative SETTLEMENT_W_VOID floor — never zero.
  • IDEMPOTENCY fails toward DOUBLE-COUNT, never skip: the realized_settled row is written
    FIRST, the processed-ledger entry SECOND. A crash between them leaves the hedge un-ledgered
    → re-scored next run → counted twice (cap halts early — annoying, SAFE). Skipping a real
    loss (cap blind) is the failure this whole component exists to prevent.
  • Inert in DRY_RUN: trades.log only gets rows in live execution, so in DRY there is nothing
    to score and the loop no-ops. Built + tested complete anyway.

It writes to logs/execution_pnl.csv (the live loss-cap file) — its legitimate job, and the ONLY
path that writes the realized_settled bucket. The offline scripts/settlement_capture.py stays
hard-barred from execution_pnl.csv (it writes the separate, hypothetical capture file).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from bot.core.logger import get_logger
from bot.kalshi.settlement import Outcome, fetch_settlement, realized_pnl_live

log = get_logger(__name__)

_SCORE_INTERVAL_S = 900.0   # settlement is hours-to-days out; a 15-min cadence is ample.


class SettlementScorer:
    """Scores executed hedges against actual settlement and writes realized_settled P&L."""

    def __init__(
        self, poly_client, kalshi_client, exec_pnl_writer, *,
        trades_path: str = "logs/trades.log",
        ledger_path: str = "logs/settled_scored.txt",
        w_void: float,
    ) -> None:
        self.poly_client = poly_client
        self.kalshi_client = kalshi_client
        self._writer = exec_pnl_writer          # csv.writer over execution_pnl.csv (line-buffered)
        self._trades_path = trades_path
        self._ledger_path = ledger_path
        self._w_void = w_void
        self._scored: set[str] = self._load_ledger()

    # ── idempotency ledger ────────────────────────────────────────────────────
    @staticmethod
    def _hedge_key(rec: dict) -> str:
        """Stable identity for one executed hedge. trades.log timestamps are microsecond-ISO
        (one per hedge); ticker+side+shares disambiguate the rare same-instant case."""
        return (f"{rec.get('timestamp')}|{rec.get('kalshi_ticker')}|"
                f"{rec.get('kalshi_side')}|{rec.get('shares')}")

    def _load_ledger(self) -> set[str]:
        keys: set[str] = set()
        if not os.path.exists(self._ledger_path):
            return keys
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    k = line.strip()
                    if k:
                        keys.add(k)
        except OSError as exc:
            log.error(f"settlement_scorer: ledger read failed: {exc}")
        return keys

    def _mark_scored(self, key: str) -> None:
        """Append to the ledger AFTER the realized_settled row is written. Ordering is the
        crash-safety guarantee: a crash here (row written, ledger not) → re-score → double-count
        (safe), never skip."""
        try:
            with open(self._ledger_path, "a", encoding="utf-8") as f:
                f.write(key + "\n")
        except OSError as exc:
            log.error(f"settlement_scorer: ledger append failed for {key}: {exc}")
        self._scored.add(key)

    # ── source: trades.log (executed hedges) ──────────────────────────────────
    def _load_hedges(self) -> list[dict]:
        """Parse executed cross-arb hedges from trades.log (permanent JSONL). Bad lines skipped."""
        hedges: list[dict] = []
        if not os.path.exists(self._trades_path):
            return hedges
        try:
            with open(self._trades_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") == "kalshi-arb" and rec.get("status") == "EXECUTED":
                        hedges.append(rec)
        except OSError as exc:
            log.error(f"settlement_scorer: trades.log read failed: {exc}")
        return hedges

    # ── scoring ───────────────────────────────────────────────────────────────
    async def score_once(self) -> int:
        """Score every not-yet-scored executed hedge whose markets have settled. Returns the
        number of realized_settled rows written. Each hedge is isolated — one failure never
        blocks the rest, and an unsettled (PENDING) hedge is simply retried next run."""
        written = 0
        for rec in self._load_hedges():
            key = self._hedge_key(rec)
            if key in self._scored:
                continue
            try:
                shares = float(rec.get("shares") or 0)
                cost = float(rec.get("cost") or 0)          # FEE-INCLUSIVE total entry cost
                poly_eff = float(rec.get("poly_ask_eff") or 0)
                if shares <= 0:
                    continue
                kalshi_eff = cost / shares - poly_eff        # fee-inclusive kalshi leg (cond. 3)

                res = await fetch_settlement(
                    self.poly_client, self.kalshi_client,
                    rec.get("poly_token"), rec.get("kalshi_ticker"), rec.get("kalshi_side"),
                )
                if res.outcome == Outcome.PENDING:
                    continue                                 # not settled yet → retry next run

                pnl = realized_pnl_live(res, shares, poly_eff, kalshi_eff, self._w_void)
                if pnl is None:
                    continue                                 # defensive; non-pending → not expected

                # 1) row FIRST, 2) ledger SECOND — crash between = double-count (safe).
                self._write_realized(res, rec, shares, pnl)
                self._mark_scored(key)
                written += 1
                log.info(
                    f"settlement_scored {res.outcome.value} {rec.get('kalshi_ticker')} "
                    f"[{rec.get('kalshi_side')}] shares={shares:.0f} pnl=${pnl:.2f}"
                )
            except Exception as exc:  # noqa: BLE001 — one bad hedge must not block the rest
                log.error(f"settlement_scorer: scoring {key} failed: {exc!r}")
        return written

    def _write_realized(self, res, rec: dict, shares: float, pnl: float) -> None:
        """Append the realized_settled row. `amount` is SIGNED P&L (negative = loss) — the cap
        (safety.cumulative_realized_loss) counts max(0, -amount). NEVER touches marked_unsettled."""
        self._writer.writerow([
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "realized_settled", res.outcome.value,
            rec.get("event", ""), rec.get("kalshi_ticker", ""), int(shares),
            f"{pnl:.4f}",
            f"poly_payout={res.poly_payout} kalshi_payout={res.kalshi_payout} {res.detail}",
        ])

    async def loop(self, interval: float = _SCORE_INTERVAL_S) -> None:
        """Background task: periodically score settled hedges. Safe to add unconditionally to
        the runner gather — inert in DRY (empty trades.log), never raises out."""
        while True:
            try:
                await self.score_once()
            except Exception as exc:  # noqa: BLE001
                log.error(f"settlement_scorer loop error: {exc!r}")
            await asyncio.sleep(interval)
