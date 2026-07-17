"""
scripts/data_audit.py — the re-runnable, N-floor-disciplined status dashboard.
────────────────────────────────────────────────────────────────────────────────────────
Institutionalizes the six-section data dive as ONE read-only command, with the verdict-
methodology discipline baked in structurally: every numeric read states its N and (where it
has one) its floor, and any metric below floor prints "INSUFFICIENT, X from floor" — NEVER a
directional or viability read. The hard floor (SETTLEMENT_MIN_HEDGED_N) governs ONLY the
verdict metric; descriptive metrics state N + a factual read, flagged `provisional` when thin.
No metric anywhere prints "suggests viable / not viable" — that is the whole point of the
script: a safe dashboard that re-runs as N accumulates without ever reading a verdict off thin
data. See docs/verdict_methodology.md.

VERDICT-DEFINITION NOTE: settled∩both_fill is computed via the SHARED verdict_counts (imported
from subsecond_calibration), so §1 here and §C there cannot drift — one definition, called twice.
It (a) excludes PENDING capture rows (a pending game is not settled), (b) EXCLUDES frozen-book
both_fills (rest_transact_age_s > config.FROZEN_BOOK_AGE_S — a stale-book phantom), and (c) counts
at GAME granularity for the floor (correlated same-game triples are one independent observation).
See docs/verdict_methodology.md.

Reuses scripts/subsecond_calibration.py (the §A decay curve + §B not_persistent logic + the
_num/_R2_CUTOVER helpers + the verdict-count helpers) rather than duplicating it. Read-only.

  Block A — both_fill vs floor (the calendar block + ETA-to-floor)
  Block B — settlement R2-freshness (does settlement_capture cover the R2 both_fills?)
  Block C — §C coverage spread (phantom fraction PER SPORT — the orderbook-fix gate)
  §1 Verdict data · §2 §C detection-quality · §3 §A decay · §4 funnel + not_persistent
  §5 Poly-staleness contamination · §6 regime hygiene + deploy-state

Run:  .venv/bin/python -m scripts.data_audit
"""
from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from statistics import median

from bot.core import config
from scripts.subsecond_calibration import (
    _num, _R2_CUTOVER, decay_curve, not_persistent_lifetimes,
    _triple, _game_key, _freshness, _in_freeze_episode, _SETTLED,
    settled_triple_keys, verdict_counts,
)

_FILL = "logs/fill_success.csv"
_REJ = "logs/rejected_edges.csv"
_CAPTURE = "logs/settlement_capture.csv"
_WF_SAMPLES = "logs/would_fire_samples.csv"
_STDOUT_LOG = "logs/bot.stdout.log"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# _SETTLED, the verdict keying (_triple/_game_key), the frozen partition (_freshness) and the
# settled∩both_fill count (verdict_counts) are imported from subsecond_calibration — ONE
# definition, shared so the two consumers cannot drift.

# The status-table accumulator: (section, metric, N, floor|"-", "above"/"below"/"-", read).
_TABLE: list[tuple] = []


def _rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _series(ticker: str) -> str:
    return (ticker or "").split("-", 1)[0]


def status(label: str, n: int, floor: int | None, read_fn) -> str:
    """The discipline. Below floor → 'INSUFFICIENT, X from floor', never a directional read.
    Above/no floor → N + the factual read. `read_fn` is a thunk so a directional statement is
    never even evaluated below floor."""
    if floor is not None and n < floor:
        return f"{label}: N={n} floor={floor} → INSUFFICIENT, {floor - n} from floor"
    return f"{label}: N={n}" + (f" floor={floor}" if floor is not None else "") + f" → {read_fn()}"


def _settlement_outcomes() -> dict[tuple, str]:
    """triple → settlement outcome (clean/void/divergence/pending). The verdict join (section_
    verdict) uses the SETTLED subset only; PENDING is explicitly excluded there."""
    return {_triple(r): r.get("outcome", "") for r in _rows(_CAPTURE)}


# ── Block A / §1 — verdict data ────────────────────────────────────────────────────────────
def section_verdict(fill: list[dict], settle: dict[tuple, str], show_frozen: bool = False) -> dict:
    floor = config.SETTLEMENT_MIN_HEDGED_N
    both = [r for r in fill if r["outcome"] == "both_fill"]
    # THE verdict denominator — computed via the SHARED verdict_counts (frozen-book both_fills
    # excluded, PENDING excluded, counted at GAME granularity for the floor). The identical call
    # in subsecond_calibration §C cannot drift from this one — single definition, called twice.
    settled_keys = {k for k, v in settle.items() if v in _SETTLED}
    vc = verdict_counts(both, settled_keys)
    n_settled = vc["n_settled_games"]          # the floor is interpreted at GAME granularity
    both_keys = {_triple(r) for r in both}     # all triples (incl. frozen) — for the Block-B coverage view

    by_day = defaultdict(set)
    for r in both:
        by_day[r["timestamp"][:10]].add(_triple(r))
    days = sorted(by_day)
    per_day = (vc["n_games"] / len(days)) if days else 0.0   # game-keyed rate (matches the floor unit)

    print("\n" + "═" * 78)
    print("§1 — VERDICT DATA (report-and-stop; NO settlement rate, NO E[settle|both_fill])")
    print("═" * 78)
    print("  " + status("settled∩both_fill (GAME-keyed; the verdict denominator)", n_settled, floor,
                         lambda: "floor reached"))
    print(f"  granularity: {n_settled} settled GAMES (the floor unit — correlated same-game triples "
          f"count once) / {vc['n_settled_triples']} settled triples; the floor {floor} is over games.")
    print(f"  fresh both_fill: {vc['n_fresh_rows']} rows / {vc['n_triples']} triples / {vc['n_games']} "
          f"games — {n_settled} settled, {vc['n_games'] - n_settled} pending/unscored")
    print(f"  EXCLUDED as stale-book phantoms (age > {config.FROZEN_BOOK_AGE_S:.0f}s): "
          f"{vc['n_frozen_rows']} frozen + {vc['n_unknown_rows']} age-unknown of {vc['n_both_rows']} "
          f"both_fill rows (reported, not deleted — pre-commits the rule before they settle).")
    print(f"  calendar: {dict((d, len(by_day[d])) for d in days)}  "
          f"→ ≈{per_day:.1f} both_fill GAMES/game-day")
    if per_day > 0 and n_settled < floor:
        eta = (floor - n_settled) / per_day
        print(f"  ETA-to-floor (a WHEN estimate, not a what): ≈{eta:.0f} more game-days at the current "
              f"detection rate to reach {floor} SETTLED games (settlement lag ~1 day not modeled).")

    # the both_fill FACTS table — raw per-row facts (incl. each row's settlement outcome + its
    # fresh/frozen flag), NEVER a computed settlement rate. Per-row outcomes yes; an aggregate
    # "X/Y clean" row would cross the line. Frozen/age-unknown rows are EXCLUDED from the verdict
    # (age > threshold — freeze-recovery + halt phantoms). By DEFAULT they're COLLAPSED to one line
    # so the fresh rows the verdict actually weighs aren't buried under a wall of ❄ phantoms (this
    # dump has misread before). Pass --show-frozen for the full per-row dump. Collapsing is
    # DISPLAY-ONLY: no row is dropped from disk or from any count — n_frozen_rows above already
    # excludes them via the shared verdict_counts; visidata/raw-CSV inspection is unaffected.
    # A row is verdict-eligible (and so shown by default) iff age-fresh AND outside a freeze
    # episode — the SAME two-part exclusion verdict_counts uses, so the shown count reconciles with
    # n_fresh_rows above. The freeze test catches recovery phantoms (fresh transactTime / stale
    # price → 70% edge) that the age test alone lets through; those get marked "fz", not blank.
    def _mark(r: dict) -> str:
        if _in_freeze_episode(r):
            return "fz"
        return {"fresh": "  ", "frozen": "❄", "unknown": "?"}[_freshness(r)]

    def _eligible(r: dict) -> bool:
        return _freshness(r) == "fresh" and not _in_freeze_episode(r)

    ordered = sorted(both, key=lambda x: x["timestamp"])
    shown = ordered if show_frozen else [r for r in ordered if _eligible(r)]
    n_collapsed = len(ordered) - len(shown)
    print(f"\n  both_fill facts ({len(shown)} of {len(both)} rows shown — facts, not a sample; "
          f"no aggregate rate):")
    print(f"    {'day':>10} {'series':>11} {'side':>4} {'edge%':>6} {'kfill':>7} "
          f"{'win_ms':>7} {'age_s':>8} {'fr':>2}  settlement")
    for r in shown:
        edge = _num(r, "edge")
        kf = _num(r, "kalshi_fillable")
        wm = _num(r, "fill_window_ms")
        age = _num(r, "rest_transact_age_s")
        fr = _mark(r)
        oc = settle.get(_triple(r), "—(absent)")
        print(f"    {r['timestamp'][:10]:>10} {_series(r['kalshi_ticker']):>11} "
              f"{r['kalshi_side']:>4} {(edge*100 if edge is not None else 0):>6.1f} "
              f"{(kf if kf is not None else 0):>7.0f} {(wm if wm is not None else 0):>7.0f} "
              f"{(age if age is not None else -1):>8.1f} {fr:>2}  {oc}")
    if n_collapsed:
        print(f"    … +{n_collapsed} frozen/age-unknown/freeze-episode rows COLLAPSED (excluded from "
              f"the verdict — freeze-recovery + halt phantoms); re-run with --show-frozen for full dump.")
    if show_frozen:
        print("  ❄ = age-frozen (>threshold); fz = freeze-episode window (incl. fresh-timestamp "
              "recovery phantoms); both EXCLUDED from the count, shown for transparency.")
    print("  hypotheses-to-watch (NOT reads): depth-skew? side-skew? — facts only at this N.")

    _TABLE.append(("§1", "settled∩both_fill (games)", n_settled, floor,
                   "below" if n_settled < floor else "above",
                   f"INSUFFICIENT, {floor - n_settled} from floor" if n_settled < floor else "floor reached"))
    return {"n_settled": n_settled, "n_settled_triples": vc["n_settled_triples"],
            "n_both": vc["n_games"], "n_triples": vc["n_triples"], "floor": floor,
            "per_day": per_day, "both_keys": both_keys,
            "n_frozen": vc["n_frozen_rows"], "n_unknown": vc["n_unknown_rows"]}


# ── Block C / §2 — §C detection-quality ────────────────────────────────────────────────────
def section_detection_quality(fill: list[dict]) -> dict:
    n = len(fill)
    mix = Counter(r["outcome"] for r in fill)
    km = [r for r in fill if r["outcome"] == "kalshi_moved"]
    phantom = [r for r in km if _num(r, "kalshi_fillable") == 0]

    print("\n" + "═" * 78)
    print("§2 — §C DETECTION-QUALITY (PRIMARY; phantom-confounded, NOT a capturability read)")
    print("═" * 78)
    print(f"  outcome mix (N={n}): {dict((k, mix[k]) for k in sorted(mix))}")
    print(f"  fill rate: {mix.get('both_fill',0)}/{n} = "
          f"{100*mix.get('both_fill',0)//n if n else 0}%  — framed as 'most detections phantom', "
          f"NOT 'X% capturable' (the rate is confounded by the ticker-phantom below).")

    pf = (f"{len(phantom)}/{len(km)}" + (f" = {100*len(phantom)//len(km)}%" if km else "")) if km \
        else "0/0 (no kalshi_moved rows yet)"
    print(f"  phantom fraction (kalshi_moved with kalshi_fillable=0): {pf}")

    # Block C — phantom PER SPORT (the gate is coverage-not-count)
    by_series_km = defaultdict(list)
    for r in km:
        by_series_km[_series(r["kalshi_ticker"])].append(r)
    per_sport = {}
    if not km:
        print("  phantom PER SPORT: no kalshi_moved rows yet — phantom coverage N=0 (nothing to read).")
    else:
        print("  phantom PER SPORT (the orderbook-fix gate — does it hold across coverage?):")
        for s in sorted(by_series_km):
            rs = by_series_km[s]
            ph = sum(1 for r in rs if _num(r, "kalshi_fillable") == 0)
            per_sport[s] = (ph, len(rs))
            print(f"    {s:>11}: {ph}/{len(rs)} phantom"
                  + (f" = {100*ph//len(rs)}%" if rs else "") + f"  ({len(rs)} kalshi_moved rows)")

    sports = sorted({_series(r["kalshi_ticker"]) for r in fill})
    fdays = sorted({r["timestamp"][:10] for r in fill})
    print(f"  COVERAGE: {len(sports)} sport(s) {sports} across {len(fdays)} game-day(s) {fdays}")

    side = Counter(r["kalshi_side"] for r in km)
    side_ph = Counter(r["kalshi_side"] for r in phantom)
    if km:
        print("  NO-side concentration of the phantom (watch as N grows):")
        for s in sorted(side):
            print(f"    side={s}: {side_ph.get(s,0)}/{side[s]} phantom")

    _TABLE.append(("§2", "phantom fraction", len(km), None, "-",
                   pf + f" — phantom data {len(per_sport)}/{len(sports)} sports"))
    return {"per_sport": per_sport, "sports": sports, "days": fdays}


# ── §5 — Poly-staleness contamination ──────────────────────────────────────────────────────
def section_poly_staleness(fill: list[dict]) -> None:
    print("\n" + "═" * 78)
    print("§5 — POLY-STALENESS CONTAMINATION CHECK (is the phantom Kalshi-only or multi-feed?)")
    print("═" * 78)
    # READ-staleness signal (the CDN-freeze concern): poly_read_latency_ms — did the cache-busted
    # HTTP read complete live? "cache_hit" = a ≤1s local-cache hit (no HTTP), not a stale read.
    lat = [v for r in fill if (v := _num(r, "poly_read_latency_ms")) is not None]
    cache_hits = sum(1 for r in fill if r.get("poly_read_latency_ms") == "cache_hit")
    if lat:
        print(f"  poly_read_latency_ms (the READ-staleness signal): N={len(lat)}  median {median(lat):.0f}ms"
              f"  max {max(lat):.0f}ms;  {cache_hits} cache_hit (≤1s local cache) → reads complete live.")

    # BOOK transactTime age: rest_transact_age_s = time since the book last transacted. A high tail is
    # AMBIGUOUS (illiquid/stable book vs frozen feed — the methodology #5 trap); the live read-latency
    # above + the active-game-window context are the INDEPENDENT disambiguators. Report the
    # distribution, do NOT assert it away.
    ages = sorted(v for r in fill if (v := _num(r, "rest_transact_age_s")) is not None)
    blank = sum(1 for r in fill if _num(r, "rest_transact_age_s") is None)
    if ages:
        p90 = ages[max(0, int(0.9 * len(ages)) - 1)]
        stale = sum(1 for a in ages if a > 30)
        sentinel = sum(1 for a in ages if a < 0)
        print(f"  rest_transact_age_s (BOOK age — AMBIGUOUS, not read freshness): N={len(ages)}  "
              f"median {median(ages):.3f}s  p90 {p90:.1f}s  max {max(ages):.1f}s;  {stale} rows >30s, "
              f"blank={blank}, sentinel(<0)={sentinel}")
        print("  → median fresh; the >30s tail is illiquid/stable books (legitimately no recent trade), "
              "distinguished from a frozen read by the live read-latency above — NOT asserted away.")
        read = (f"read median {median(lat):.0f}ms live; " if lat else "") + \
               f"book-age median {median(ages):.3f}s, {stale} >30s (illiquid tail)"
    else:
        print("  rest_transact_age_s: no parseable rows.")
        read = "no data"

    k_unfill = sum(1 for r in fill if _num(r, "kalshi_fillable") == 0)
    p_unfill = sum(1 for r in fill if _num(r, "poly_fillable") == 0)
    n = len(fill)
    print(f"  multi-venue phantom split over all {n} fill rows: "
          f"kalshi_fillable=0 in {k_unfill} ({100*k_unfill//n if n else 0}%), "
          f"poly_fillable=0 in {p_unfill} ({100*p_unfill//n if n else 0}%)")
    print("  → Kalshi-dominant ⇒ orderbook-sourced Kalshi detection RELOCATES the residual (Poly leg), "
          "does not fully eliminate it.")
    _TABLE.append(("§5", "fill-read freshness", len(ages), None, "-", read))


# ── §4 — reject funnel (not_persistent handled by the reused §B) ────────────────────────────
def section_funnel(rej: list[dict]) -> None:
    print("\n" + "═" * 78)
    print("§4 — REJECT FUNNEL + not_persistent (gate distribution; confirm-window placement)")
    print("═" * 78)
    reasons = Counter(r["reason"] for r in rej)
    n = len(rej)
    print(f"  reject funnel (N={n}), ordered:")
    for reason, c in reasons.most_common():
        print(f"    {reason:>20}: {c:>4}  ({100*c//n if n else 0}%)")
    _TABLE.append(("§4", "reject funnel", n, None, "-",
                   "; ".join(f"{k}={v}" for k, v in reasons.most_common(3))))
    # not_persistent lifetimes + the confirm-window placement check are computed by the reused §B
    # function (reads rejected_edges + the LIVE config.KALSHI_CONFIRM_SECONDS, prints the histogram
    # + threshold sensitivity). It prints no settled count, so it's off the verdict path.
    not_persistent_lifetimes()


# ── §6 — regime hygiene + deploy-state ──────────────────────────────────────────────────────
def section_regime(fill: list[dict], rej: list[dict]) -> None:
    print("\n" + "═" * 78)
    print("§6 — REGIME HYGIENE + DEPLOY-STATE")
    print("═" * 78)
    for label, rows in (("fill_success", fill), ("rejected_edges", rej)):
        pre = sum(1 for r in rows if r.get("timestamp", "") < _R2_CUTOVER)
        print(f"  {label}: {len(rows)} rows, {pre} pre-R2 "
              + ("✓ clean" if pre == 0 else "⚠️ MIXED REGIME — filter ≥ cutover"))
    cap = _rows(_CAPTURE)
    print(f"  settlement_capture: {len(cap)} rows — SPANS pre-R2 by design (the 06-20/22 baseline is "
          f"kept via keep_rows; the verdict join filters to the R2 both_fills, so the pre-R2 rows are "
          f"inert for the conditional).")
    print(f"  rotated archives: logs/rotated/ holds the pre-cutover contaminated data, segregated by "
          f"the fix that rotated them — before/after context only, never a verdict read.")

    print("  deploy-state (are the committed fixes live?):")
    try:
        head = subprocess.run(["git", "log", "-1", "--format=%h %s"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
        print(f"    HEAD = {head}")
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"    HEAD = (git read failed: {exc})")
    if os.path.exists(_STDOUT_LOG):
        with open(_STDOUT_LOG, errors="replace") as f:
            lines = [_ANSI.sub("", ln).rstrip("\n") for ln in f]
        starts = [i for i, ln in enumerate(lines) if "DRY RUN mode enabled" in ln]
        if starts:
            si = starts[-1]
            t = lines[si][:8] if re.match(r"\d\d:\d\d:\d\d", lines[si]) else "?"
            after = lines[si:]
            drift = sum(1 for ln in after if "SCHEMA DRIFT" in ln)
            recon = sum(1 for ln in after if "forcing reconnect" in ln)
            last_t = next((lines[i][:8] for i in range(len(lines) - 1, -1, -1)
                           if re.match(r"\d\d:\d\d:\d\d", lines[i])), "?")
            print(f"    last bot restart at {t} (log time, HH:MM:SS only — no date, so this is "
                  f"ambiguous if the log spans days); log runs to {last_t}.")
            print(f"    post-restart: schema-drift={drift}, watchdog force-reconnect={recon}")
            print(f"    → force-reconnect interpretation: if these occurred during OFF-HOURS quiet, the "
                  f"active-game-window gate is not yet live (restart needed); if during LIVE games, they "
                  f"may be correct freeze-catches. Check their timestamps against game windows — the "
                  f"script counts them but can't tell the two apart.")
        else:
            print(f"    no startup banner in {_STDOUT_LOG} (rotated?) — restart state unknown.")
    else:
        print(f"    {_STDOUT_LOG} absent — cannot read deploy-state.")
    clean = all(r.get("timestamp", "") >= _R2_CUTOVER for r in fill + rej)
    _TABLE.append(("§6", "regime cleanliness", len(fill) + len(rej), None, "-",
                   "fill_success + rejected 100% R2-clean" if clean else "MIXED — filter needed"))


# ── headline blocks + the final status table ───────────────────────────────────────────────
def print_blocks(verdict: dict, dq: dict, settle: dict[tuple, str]) -> None:
    print("═" * 78)
    print("DATA AUDIT — three gating blocks (lead with these)")
    print("═" * 78)
    f, ns = verdict["floor"], verdict["n_settled"]   # ns = settled GAMES (the floor unit)
    print(f"  BLOCK A  both_fill vs floor: {ns}/{f} settled GAMES "
          + (f"→ INSUFFICIENT, {f-ns} from floor; ≈{(f-ns)/verdict['per_day']:.0f} more game-days at "
             f"{verdict['per_day']:.1f}/day" if ns < f and verdict["per_day"] else "→ FLOOR REACHED")
          + f"  ({verdict['n_settled_triples']} settled triples; {verdict['n_frozen']} frozen + "
            f"{verdict['n_unknown']} age-unknown excluded)")
    # Block B is settlement COVERAGE — triple-keyed (does each both_fill triple have a capture row?).
    n_triples_total = len(verdict["both_keys"])
    in_capture = sum(1 for k in verdict["both_keys"] if k in settle)
    pending = sum(1 for k in verdict["both_keys"] if settle.get(k) == "pending")
    if in_capture == 0:
        print(f"  BLOCK B  settlement R2-freshness: ⚠️ STALE — 0 of {n_triples_total} both_fill triples "
              f"are in settlement_capture → settled∩both_fill structurally 0 until re-run.")
    else:
        print(f"  BLOCK B  settlement R2-freshness: FRESH — {in_capture}/{n_triples_total} both_fill "
              f"triples covered ({verdict['n_settled_triples']} settled, {pending} pending awaiting "
              f"game resolution).")
    if not dq["per_sport"]:
        print("  BLOCK C  §C coverage / phantom per sport: no kalshi_moved rows yet — phantom coverage N=0.")
    else:
        spread = ", ".join(f"{s} {ph}/{tot}" for s, (ph, tot) in sorted(dq["per_sport"].items()))
        n_ph_sports, n_sports = len(dq["per_sport"]), len(dq["sports"])
        print(f"  BLOCK C  §C coverage / phantom per sport: {spread}  "
              f"(phantom data for {n_ph_sports} of {n_sports} covered sports {dq['sports']}, "
              f"{len(dq['days'])} game-days)")
        if n_ph_sports < n_sports:
            print(f"    → across-coverage gate NOT yet met: phantom data exists for only {n_ph_sports}/"
                  f"{n_sports} sports (the rest have no kalshi_moved rows). General-vs-sport-specific "
                  f"is unresolvable until every covered sport has kalshi_moved data.")
        else:
            print("    → gate 'phantom holds across coverage': read per-sport above — general vs sport-specific.")


def print_table() -> None:
    print("\n" + "═" * 78)
    print("PER-SECTION STATUS TABLE")
    print("═" * 78)
    print(f"  {'sec':>4} {'metric':>32} {'N':>5} {'floor':>6} {'pos':>6}  read")
    for sec, metric, n, floor, pos, read in _TABLE:
        fl = str(floor) if floor is not None else "-"
        print(f"  {sec:>4} {metric:>32} {n:>5} {fl:>6} {pos:>6}  {read}")
    print("\n  Discipline: the hard floor (SETTLEMENT_MIN_HEDGED_N) governs ONLY the verdict metric;")
    print("  descriptive rows state N + a factual read. NO viability conclusion is drawn anywhere.")


def main() -> None:
    show_frozen = "--show-frozen" in sys.argv
    fill = _rows(_FILL)
    rej = _rows(_REJ)
    settle = _settlement_outcomes()

    verdict = section_verdict(fill, settle, show_frozen)   # §1 (the ONLY settled-count source)
    dq = section_detection_quality(fill)         # §2

    print("\n")
    print_blocks(verdict, dq, settle)            # Blocks A/B/C headline

    print("\n" + "═" * 78)
    print("§3 — §A SUB-SECOND DECAY (reused from subsecond_calibration)")
    print("═" * 78)
    decay_curve()                                # §3 (prints its own N + PROVISIONAL flag)
    n_wf = len({r["wf_id"] for r in _rows(_WF_SAMPLES)})
    _TABLE.append(("§3", "decay curve (wf_ids)", n_wf, None, "-",
                   ("provisional — " if n_wf < 30 else "") + "median-fillable, tail-vulnerable"))

    section_funnel(rej)                          # §4 (+ reused §B not_persistent)
    section_poly_staleness(fill)                 # §5
    section_regime(fill, rej)                    # §6

    print_table()


if __name__ == "__main__":
    main()
