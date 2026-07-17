"""
scripts/explain_edge.py
───────────────────────
Dump everything about a detected edge across all the diagnostic tables, joined and
labeled, so you (or Claude) can judge whether it was REAL and CAPTURABLE.

Pulls, for a market keyword (substring of event / slug / ticker, case-insensitive)
or a specific --wf-id:
  • would_fire.csv         — the gated fire-time snapshot (price, depth, fillable)
  • would_fire_samples.csv — book at +0/1/2/3s (did the edge hold or evaporate?)
  • positive_edges.csv   — lifecycle START/SAMPLE/END (peak edge + duration)
  • edge_freshness.csv          — poly_age / kalshi_age (freshness = real vs stale phantom)
  • bot.stdout.log         — the "Kalshi arb:" fire line (poly_age inline)

Read-only. Run:
  .venv/bin/python -m scripts.explain_edge czersa
  .venv/bin/python -m scripts.explain_edge --wf-id 7
  .venv/bin/python -m scripts.explain_edge tor-bos --log logs/bot.stdout.log.1
"""
from __future__ import annotations

import argparse
import csv
import os

_WF = "logs/would_fire.csv"
_WFS = "logs/would_fire_samples.csv"
_PEDGE = "logs/positive_edges.csv"
_AEDGE = "logs/edge_freshness.csv"
_STDOUT = "logs/bot.stdout.log"

# Columns whose text we match the keyword against (per file).
_MATCH_COLS = ("event", "event_title", "poly_token", "kalshi_ticker", "game")


def _rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _matches(row: dict, kw: str) -> bool:
    return any(kw in (row.get(c, "") or "").lower() for c in _MATCH_COLS)


def _print_rows(title: str, rows: list[dict]) -> None:
    print(f"\n── {title} ({len(rows)}) ──")
    for r in rows:
        print("  " + "  ".join(f"{k}={v}" for k, v in r.items() if v != ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Explain a detected edge across all tables")
    ap.add_argument("keyword", nargs="?", default="",
                    help="substring of event/slug/ticker (case-insensitive)")
    ap.add_argument("--wf-id", type=int, default=None, help="target a specific would_fire id")
    ap.add_argument("--log", default=_STDOUT, help="stdout log to scan for the fire line")
    args = ap.parse_args()
    kw = args.keyword.lower()

    if not kw and args.wf_id is None:
        ap.error("give a keyword or --wf-id")

    # 1. would_fire — the gated snapshot. Pick by wf_id or keyword.
    wf = _rows(_WF)
    if args.wf_id is not None:
        wf_hits = [r for r in wf if r.get("wf_id") == str(args.wf_id)]
    else:
        wf_hits = [r for r in wf if _matches(r, kw)]
    _print_rows("WOULD-FIRE (gated snapshot)", wf_hits)

    # 2. samples for those wf_ids (the capturability signal). NOTE: wf_id is only
    # unique across restarts for data written after the monotonic-id fix; older
    # rows may collide — flagged so the join isn't trusted blindly.
    ids = {r.get("wf_id") for r in wf_hits if r.get("wf_id")}
    if ids:
        samples = [r for r in _rows(_WFS) if r.get("wf_id") in ids]
        _print_rows("BOOK SAMPLES +0/1/2/3s (did it hold?)", samples)
        dupe = [i for i in ids if sum(1 for r in wf if r.get("wf_id") == i) > 1]
        if dupe:
            print(f"  ⚠️ wf_id {sorted(dupe)} appears >1× in would_fire (pre-fix restart "
                  f"collision) — sample join may mix edges.")

    # 3. lifecycle (duration + peak).
    _print_rows("LIFECYCLE (peak edge + duration)", [r for r in _rows(_PEDGE) if _matches(r, kw)])

    # 4. freshness — the real-vs-phantom tell.
    ae = [r for r in _rows(_AEDGE) if _matches(r, kw)]
    _print_rows("FRESHNESS (poly_age / kalshi_age)", ae)
    stale = [r for r in ae if _f(r.get("poly_age_s")) is not None and _f(r["poly_age_s"]) > 1.0]
    if stale:
        print(f"  ⚠️ {len(stale)} row(s) with poly_age > 1s — likely stale-lag phantom, not a real arb.")

    # 5. the fire line (poly_age inline).
    print(f"\n── FIRE LOG ({args.log}) ──")
    if kw and os.path.exists(args.log):
        n = 0
        with open(args.log, errors="ignore") as f:
            for line in f:
                if "Kalshi arb:" in line and kw in line.lower():
                    print("  " + line.rstrip())
                    n += 1
        if not n:
            print("  (no matching 'Kalshi arb:' fire line)")
    else:
        print("  (skipped — need a keyword and an existing log)")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
