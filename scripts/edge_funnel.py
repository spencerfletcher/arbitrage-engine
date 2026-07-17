"""
scripts/edge_funnel.py — where do detected edges die before they fire?
──────────────────────────────────────────────────────────────────────
Read-only aggregate over the three observability ledgers to quantify the
detected → would_fire funnel and surface whether any gate mis-kills viable edges:

  • positive_edges.csv — every positive crossing (START), incl. sub-min-edge
  • rejected_edges.csv — the complete non-fire ledger, one row per (reason)
  • would_fire.csv     — edges that cleared EVERY gate

It prints: how many edges were detected, the rejection histogram by reason
(detection-level: below_min_edge / not_persistent / dominated …; fire-gate:
thin_entry_depth / poly_not_fillable / stale_price / book_suspect …), and how
many reached would_fire. Covers the standing "Watch — gate-bug check on
rejected_edges" TODO: if thin_entry_depth rows show adequate depth, or stale_price
shows tiny ages, a gate is wrongly killing a viable edge.

Run:  .venv/bin/python -m scripts.edge_funnel
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_POS = "logs/positive_edges.csv"
_REJ = "logs/rejected_edges.csv"
_WF = "logs/would_fire.csv"

# Reasons grouped by funnel stage (detection-level happen before the fire gates).
_DETECTION = {"below_min_edge", "not_persistent", "dominated_by_better_direction",
              "duplicate_position", "cooldown", "exposure_cap", "paused"}
_FIRE_GATE = {"extreme_band", "book_suspect", "stale_price", "no_exit_liquidity",
              "thin_entry_depth", "poly_not_fillable", "poly_state_unavailable"}


def _rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _num(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def main() -> None:
    pos = _rows(_POS)
    rej = _rows(_REJ)
    wf = _rows(_WF)

    # NOTE: in positive_edges.csv the 3rd column (header 'event') actually holds the
    # lifecycle marker START/SAMPLE/END; the matchup is the 'game' column. Count START
    # rows only (SAMPLE/END are the same edge re-logged), dedup games by 'game'.
    starts = [r for r in pos if (r.get("event") or "").strip() == "START"]
    detected = len(starts)
    by_reason = Counter(r.get("reason", "?") for r in rej)

    print("══ edge funnel (detected → would_fire) ══")
    print(f"detected positive crossings (positive_edges START rows): {detected}")
    print(f"distinct games: {len({r.get('game') for r in starts if r.get('game')})}")
    print(f"would_fire rows (cleared EVERY gate): {len(wf)}")

    print("\n── rejections by reason ──")
    det_total = fire_total = 0
    for reason, n in by_reason.most_common():
        stage = "detection" if reason in _DETECTION else ("fire-gate" if reason in _FIRE_GATE else "other")
        if reason in _DETECTION:
            det_total += n
        elif reason in _FIRE_GATE:
            fire_total += n
        print(f"  {reason:32s} {n:5d}   [{stage}]")
    print(f"\n  detection-level rejects: {det_total}    fire-gate rejects: {fire_total}")

    # Gate-bug check: thin_entry_depth rows whose logged kalshi_depth already covers shares,
    # or stale_price rows with tiny ages — either means a gate is wrongly killing a viable edge.
    suspicious_thin = [
        r for r in rej if r.get("reason") == "thin_entry_depth"
        and (_num(r.get("kalshi_depth")) or 0) >= (_num(r.get("shares")) or 1e9)
    ]
    suspicious_stale = [
        r for r in rej if r.get("reason") == "stale_price"
        and "max=" in (r.get("detail") or "")
        and max(_num(r.get("poly_age_s")) or 0, _num(r.get("kalshi_age_s")) or 0) <= 1.0
    ]
    print("\n── gate-bug audit (rejections that look wrong) ──")
    print(f"  thin_entry_depth with adequate logged kalshi_depth: {len(suspicious_thin)}")
    print(f"  stale_price with sub-1s ages                       : {len(suspicious_stale)}")
    if suspicious_thin:
        ex = suspicious_thin[0]
        print(f"    e.g. {ex.get('kalshi_ticker')} depth={ex.get('kalshi_depth')} shares={ex.get('shares')} detail={ex.get('detail')}")

    # Edge-size context for the survivors-vs-detected gap (informs whether downsizing
    # thin-depth rejects is worth it — those are the Phase-2 candidates).
    thin = [r for r in rej if r.get("reason") == "thin_entry_depth"]
    if thin:
        edges = sorted(e for r in thin if (e := _num(r.get("edge"))) is not None)
        if edges:
            mid = edges[len(edges) // 2]
            print(f"\n  thin_entry_depth edges: n={len(edges)} "
                  f"median={mid:.4f} max={edges[-1]:.4f} "
                  f"(Phase-2 downsize-to-fillable candidates)")


if __name__ == "__main__":
    main()
