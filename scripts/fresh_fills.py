#!/usr/bin/env python3
"""Filter a bot CSV down to trustworthy rows and emit it on stdout — for piping into visidata:

    PYTHONPATH=. .venv/bin/python scripts/fresh_fills.py [logs/<file>.csv] | visidata -f csv

Default file is logs/fill_success.csv. DISPLAY/EXPLORATION ONLY: reads the file, writes a filtered
copy to stdout; the source is never touched (same retain-the-rows discipline as
docs/data_regimes.md — we hide the spam, we don't delete it). Keeps ALL columns and (for fill_success)
all outcome buckets. Drops rows that aren't trustworthy, using three filters keyed off the SAME
definitions the verdict uses (imported, so this view can't drift):

  1. R2 cutover     — drop rows before _R2_CUTOVER (pre-all-fixes contaminated; data_regimes.md).
  2. freeze episode — drop rows inside a known feed-wide Poly freeze window (catches fresh-timestamp
                      recovery phantoms the age test misses).
  3. frozen book    — ONLY for files that carry a transact-age column (rest_transact_age_s /
                      poly_transact_age_s): drop age > FROZEN_BOOK_AGE_S and blank/unparseable age.

(1) and (2) are timestamp-keyed and apply to any log with a `timestamp` column; (3) is skipped for
schemas without an age column (e.g. rejected_edges, positive_edges) rather than nuking every row.
A one-line summary of what was dropped goes to stderr (won't pollute the CSV on stdout).
"""
from __future__ import annotations

import csv
import os
import sys

from bot.core import config
from scripts.subsecond_calibration import _num, _R2_CUTOVER, _in_freeze_episode

_AGE_COLS = ("rest_transact_age_s", "poly_transact_age_s")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve(arg: str) -> str:
    """Find the file whether the caller is in the repo root, logs/, or elsewhere: try the path as
    given (CWD-relative or absolute), then under the repo root, then repo/logs/<basename>."""
    for cand in (arg, os.path.join(_REPO, arg), os.path.join(_REPO, "logs", os.path.basename(arg))):
        if os.path.exists(cand):
            return cand
    return arg  # not found anywhere — let open() raise a clear error on what was asked for


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_REPO, "logs/fill_success.csv")
    path = _resolve(arg)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        rows = list(reader)

    has_ts = "timestamp" in fields
    age_col = next((c for c in _AGE_COLS if c in fields), None)

    def keep(r: dict) -> bool:
        if has_ts:
            ts = r.get("timestamp", "") or ""
            if ts < _R2_CUTOVER:          # filter 1 — pre-R2 contaminated
                return False
            if _in_freeze_episode(r):     # filter 2 — feed-wide freeze window
                return False
        if age_col:                       # filter 3 — frozen book (age-bearing schemas only)
            age = _num(r, age_col)
            if age is None or age > config.FROZEN_BOOK_AGE_S:
                return False
        return True

    kept = [r for r in rows if keep(r)]

    applied = ["R2-cutover", "freeze-window"] if has_ts else []
    applied += [f"frozen-book({age_col})"] if age_col else []
    print(f"fresh_fills: {path} → kept {len(kept)}/{len(rows)} rows; "
          f"filters: {', '.join(applied) or 'none (no timestamp/age columns)'}", file=sys.stderr)

    writer = csv.DictWriter(sys.stdout, fieldnames=fields)
    writer.writeheader()
    writer.writerows(kept)


if __name__ == "__main__":
    main()
