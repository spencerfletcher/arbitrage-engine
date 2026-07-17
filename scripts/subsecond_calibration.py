"""
scripts/subsecond_calibration.py — calibrate the sub-second-window constants from logs.

Read-only. Grounds two assumptions against real data, RE-RUNNABLE as the clean-regime data
accumulates — today's N is small, so treat the outputs as provisional SHAPES, not final
measurements (a decay curve from a handful of would-fire rows is a shape, not a number).

  §A  Fill-race decay curve (would_fire_samples.csv) — how the unwind bid / implied edge /
      Kalshi fillability decay across the +0/0.25/0.5/1/2/3s sample offsets. Grounds
      scripts/settlement_backtest.py:_RTT_OFFSET (the flatten-cost sample offset) AND
      quantifies the edge's sub-second half-life — the verdict-relevant read is
      "median-fillable, tail-vulnerable": capture depends on hitting the fast end of the
      fill-latency distribution, since the edge is actively decaying inside the fill window.
  §B  not_persistent lifetime histogram (rejected_edges.csv) — how long edges that fail the
      KALSHI_CONFIRM_SECONDS persistence gate actually lived. Informs the confirm window (0.3s).
  §C  Fill outcomes (fill_success.csv) — the LEADING indicator of the verdict (moves before any
      settlement): both-fill rate + which leg legs out + the detection-quality fraction (kalshi_moved
      with kalshi_fillable=0 = a ticker quote the orderbook doesn't back, NOT a fill-race loss) +
      progress toward the settled-both-fill verdict denominator. See docs/verdict_methodology.md: a
      high both-fill rate is not good news on its own, and the rate is currently CONFOUNDED by the
      ticker-phantom (so a low rate is not yet a capturability verdict).

Run:  .venv/bin/python -m scripts.subsecond_calibration
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from statistics import median, mean
from pathlib import Path

from bot.core import config

_SAMPLES_CSV = Path("logs/would_fire_samples.csv")
_REJECTS_CSV = Path("logs/rejected_edges.csv")
_FILL_CSV = Path("logs/fill_success.csv")
_CAPTURE_CSV = Path("logs/settlement_capture.csv")
_R2_CUTOVER = "2026-06-24T00:03:49Z"   # post-all-fixes regime start (docs/data_regimes.md)
_LIVED_RE = re.compile(r"lived (\d+\.\d+)s")

# Nominal sampler offsets are 0/0.25/0.5/1/2/3s, but the logged offset_s is the ACTUAL elapsed
# time (REST latency drifts it), so bucket by RANGE, not exact value.
_OFFSET_BUCKETS = [(0.0, 0.12, "~0"), (0.12, 0.37, "~0.25"), (0.37, 0.75, "~0.5"),
                   (0.75, 1.5, "~1"), (1.5, 2.5, "~2"), (2.5, 4.0, "~3")]


def _num(row: dict, key: str):
    """Parsed float for key, or None for blank/missing/garbage (blank != 0)."""
    v = row.get(key, "")
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _bucket(offset: float) -> str | None:
    for lo, hi, label in _OFFSET_BUCKETS:
        if lo <= offset < hi:
            return label
    return None


_SETTLED = ("clean", "void", "divergence")   # PENDING is NOT settled


def _triple(row: dict) -> tuple:
    """The execution-unit key: (poly_token, kalshi_ticker, kalshi_side). The settlement join
    keys on the full triple — side included — so a yes-side fill never matches a no-side
    settlement row of the same market."""
    return (row.get("poly_token"), row.get("kalshi_ticker"), row.get("kalshi_side"))


def _game_key(row: dict) -> str:
    """Collapse a triple to its physical GAME for the verdict-N (the floor is interpreted at
    game granularity — 3 triples of one game share ONE settlement outcome, so they are
    correlated, not independent; triple-N inflates apparent N toward SETTLEMENT_MIN_HEDGED_N).

    The key is the Kalshi game-ticker STEM (everything before the final '-TEAM' segment) — it
    carries date+time+matchup, so it uniquely identifies the game. That stem collapses BOTH the
    yes/no side AND the ::short↔canonical poly_token split in one move: both poly variants of a
    game pair to the SAME Kalshi game, so keying on the stem needs no separate poly
    normalization. This is deliberately robust on the catastrophic-correctness axis the dive
    flagged — it CANNOT under-collapse on a ::short variant (it ignores the poly token) and
    CANNOT over-collapse two real games (the stem is per-game-unique; a doubleheader differs in
    the time field). Fail-safe: a ticker with no '-' returns unchanged → its own key, never
    merged with another game's."""
    ticker = row.get("kalshi_ticker", "") or ""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _freshness(row: dict) -> str:
    """'fresh' | 'frozen' | 'unknown' for a both_fill row's book.  Only 'fresh' counts toward
    the verdict. age > config.FROZEN_BOOK_AGE_S = an origin-stale book (the edge was manufactured
    by a frozen quote — same defect class as a kalshi_fillable=0 phantom) → excluded. A
    blank/unparseable age means we CANNOT confirm the book was fresh → 'unknown', excluded
    conservatively (NOT silently counted as fresh) and tallied separately so it stays visible."""
    age = _num(row, "rest_transact_age_s")
    if age is None:
        return "unknown"
    return "frozen" if age > config.FROZEN_BOOK_AGE_S else "fresh"


# Known feed-wide Poly origin-freeze episodes. both_fill rows logged in these windows are excluded
# from the verdict WHOLESALE — regardless of age OR edge. Rationale: during a feed-wide freeze the
# freshness signal ITSELF is untrustworthy. The 2026-06-25 22:03–22:30 freeze (66 slugs, auth API
# also down — CF 504) republished books at RECOVERY with a FRESH rest_transact_age_s (~0.4s) but a
# STALE price → 70% phantom edges (wf 44/45) that read "fresh" and defeat both G1 and G2. So distrust
# the whole demonstrably-broken venue period rather than salvage individual rows by a signal that
# failed in this exact window. A deliberate, CONSERVATIVE over-exclusion: it may drop legitimate
# both_fills on non-frozen markets in the window — accepted (the verdict is N-starved, but trusting a
# failed freshness signal is worse). Window+rationale recorded in docs/data_regimes.md.
_FREEZE_EPISODE_WINDOWS = [("2026-06-25T22:03:00Z", "2026-06-25T22:30:00Z")]


def _in_freeze_episode(row: dict) -> bool:
    """True iff the row's timestamp is in a known feed-wide Poly freeze window. ISO lexical-UTC
    compare (all timestamps are Z-suffixed UTC — same trick as the R2 cutover); start inclusive,
    end EXCLUSIVE so a row at the window's end boundary is not swept in."""
    ts = row.get("timestamp", "") or ""
    return any(lo <= ts < hi for lo, hi in _FREEZE_EPISODE_WINDOWS)


def settled_triple_keys(capture_rows: list[dict]) -> set[tuple]:
    """The set of SETTLED triple-keys from settlement_capture — PENDING is NOT settled.
    Counting pending-as-settled overcounts the verdict denominator toward SETTLEMENT_MIN_HEDGED_N
    → a false go-readiness signal, exactly what the verdict gate must not emit. (A game in
    would_fire/rejected/fill_success that hasn't resolved is logged PENDING by
    scripts.settlement_capture and re-fetched on a later run.)"""
    return {_triple(c) for c in capture_rows if c.get("outcome") in _SETTLED}


def verdict_counts(both_rows: list[dict], settled_keys: set[tuple]) -> dict:
    """settled∩both_fill — ONE definition shared by every consumer (scripts.data_audit §1 and
    scripts.subsecond_calibration §C) so the two cannot drift (the cross-consumer agreement the
    pending-as-settled bug taught us to enforce: implement once, call twice).

    Rules (docs/verdict_methodology.md — "what to compute when settlement N arrives"):
      • Freeze-episode exclusion (runs FIRST, wholesale) — both_fill rows in a known feed-wide Poly
        freeze window (_FREEZE_EPISODE_WINDOWS) are dropped regardless of age/edge: the freshness
        signal is untrustworthy during a freeze (recovery phantoms read 'fresh'). Conservative
        over-exclusion; see docs/data_regimes.md.
      • Frozen-book exclusion — a both_fill ROW with rest_transact_age_s > FROZEN_BOOK_AGE_S is a
        stale-book phantom, excluded from the denominator (reported separately, not deleted);
        blank age → 'unknown', also excluded conservatively.  A triple/game counts as present iff
        it has ≥1 FRESH row.
      • Game granularity — the floor is interpreted over distinct GAMES (correlated triples of one
        game are one independent observation); both triple- and game-N are returned.
      • settled = capture outcome in {clean,void,divergence}; join on the full triple. A GAME is
        settled iff any of its FRESH triples joins a settled capture row.

    `settled_keys` = settled_triple_keys(capture_rows)."""
    # Freeze-episode exclusion runs FIRST and WHOLESALE — a feed-wide freeze makes the per-row
    # freshness signal untrustworthy, so we drop the whole window regardless of age/edge (see
    # _FREEZE_EPISODE_WINDOWS). The fresh/frozen/unknown partition then runs on the remainder only.
    excluded = [r for r in both_rows if _in_freeze_episode(r)]
    scored = [r for r in both_rows if not _in_freeze_episode(r)]
    fresh = [r for r in scored if _freshness(r) == "fresh"]
    frozen = [r for r in scored if _freshness(r) == "frozen"]
    unknown = [r for r in scored if _freshness(r) == "unknown"]

    fresh_triples = {_triple(r) for r in fresh}
    fresh_games = {_game_key(r) for r in fresh}
    settled_triples = {t for t in fresh_triples if t in settled_keys}
    settled_games = {_game_key(r) for r in fresh if _triple(r) in settled_keys}

    return {
        "n_both_rows": len(both_rows),
        "n_freeze_excluded_rows": len(excluded),
        "n_fresh_rows": len(fresh), "n_frozen_rows": len(frozen), "n_unknown_rows": len(unknown),
        "n_triples": len(fresh_triples), "n_games": len(fresh_games),
        "n_settled_triples": len(settled_triples), "n_settled_games": len(settled_games),
        "fresh_triples": fresh_triples, "fresh_games": fresh_games,
        "settled_triples": settled_triples, "settled_games": settled_games,
    }


def decay_curve() -> None:
    """§A — per-offset decay of the flatten bid, implied edge, and leg fillability vs t0."""
    if not _SAMPLES_CSV.exists():
        print(f"§A decay curve: {_SAMPLES_CSV} not found — skipping.")
        return
    rows = list(csv.DictReader(_SAMPLES_CSV.open()))
    by_wf: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_wf[r["wf_id"]].append(r)

    agg: dict[str, dict[str, list[float]]] = {lab: defaultdict(list) for _, _, lab in _OFFSET_BUCKETS}
    for samps in by_wf.values():
        samps = sorted(samps, key=lambda s: _num(s, "offset_s") or 0.0)
        t0 = samps[0]
        pb0, pa0, ka0 = _num(t0, "poly_bid"), _num(t0, "poly_ask"), _num(t0, "kalshi_ask")
        kf0, rd0 = _num(t0, "kalshi_fillable"), _num(t0, "rest_poly_depth")
        for s in samps:
            off = _num(s, "offset_s")
            lab = _bucket(off) if off is not None else None
            if lab is None:
                continue
            pb, pa, ka = _num(s, "poly_bid"), _num(s, "poly_ask"), _num(s, "kalshi_ask")
            kf, rd = _num(s, "kalshi_fillable"), _num(s, "rest_poly_depth")
            if pb is not None and pb0 is not None:
                agg[lab]["polybid_drop_c"].append((pb0 - pb) * 100)          # ¢ the unwind bid fell
            if None not in (pa, ka, pa0, ka0):
                agg[lab]["edge_drift_c"].append(((pa + ka) - (pa0 + ka0)) * 100)  # ¢ combined-ask rise
            if kf is not None and kf0:
                agg[lab]["kfill_ret_pct"].append(100 * kf / kf0)
            if rd is not None and rd0:
                agg[lab]["rdepth_ret_pct"].append(100 * rd / rd0)

    n_wf = len(by_wf)
    print(f"§A  Fill-race decay curve — {n_wf} wf_ids, {len(rows)} samples "
          f"({'PROVISIONAL — small N' if n_wf < 30 else 'N≥30'})")
    print(f"  {'offset':>7} {'n':>3} {'polybid_drop¢':>13} {'edge_drift¢':>12} "
          f"{'kfill_ret%':>11} {'rdepth_ret%':>12}")
    for _, _, lab in _OFFSET_BUCKETS:
        a = agg[lab]
        def med(k: str) -> str:
            v = a.get(k, [])
            return f"{median(v):>6.1f}" if v else "   —  "
        n = max((len(a.get(k, [])) for k in ("polybid_drop_c", "kfill_ret_pct")), default=0)
        print(f"  {lab:>7} {n:>3} {med('polybid_drop_c'):>13} {med('edge_drift_c'):>12} "
              f"{med('kfill_ret_pct'):>11} {med('rdepth_ret_pct'):>12}")
    print("  → polybid_drop¢ = how far the unwind bid fell vs t0 (feeds flatten cost; _RTT_OFFSET picks")
    print("    the offset). edge_drift¢ = combined-ask rise (edge collapse). *_ret% = fillable retained.")
    print("    Read: edge decays sub-second — capture is median-fillable, tail-vulnerable.")


def not_persistent_lifetimes() -> None:
    """§B — how long edges that FAILED the persistence gate actually lived, vs the confirm
    window. Lowering the window admits edges that died sooner → strand risk (you'd fire on an
    edge that vanishes before the hedge lands), so the window must stay ABOVE the real fill
    round-trip — which is DRY-unmeasured. This is the read behind 'keep 0.3 until tiny-live'."""
    if not _REJECTS_CSV.exists():
        print(f"\n§B not_persistent lifetimes: {_REJECTS_CSV} not found — skipping.")
        return
    confirm = config.KALSHI_CONFIRM_SECONDS
    lives = [
        float(m.group(1))
        for r in csv.DictReader(_REJECTS_CSV.open())
        if r.get("reason") == "not_persistent" and (m := _LIVED_RE.search(r.get("detail", "")))
    ]
    print(f"\n§B  not_persistent edge lifetimes — confirm window = {confirm}s, "
          f"{len(lives)} rows ({'PROVISIONAL — small N' if len(lives) < 100 else 'N≥100'})")
    if not lives:
        print("  (no parseable not_persistent rows)")
        return
    print(f"  lifetime: min {min(lives):.2f}  median {median(lives):.2f}  "
          f"mean {mean(lives):.3f}  max {max(lives):.2f}")
    for lo, hi in [(0, .05), (.05, .10), (.10, .15), (.15, .20), (.20, .25), (.25, .31)]:
        c = sum(1 for x in lives if lo <= x < hi)
        print(f"    {lo:.2f}-{hi:.2f}s: {c:>3} {'#' * c}")
    print("  threshold sensitivity — # of these rejected edges that would FIRE if confirm lowered")
    print("  (each then dies at thr–window → strand risk if fill round-trip > remaining life):")
    for thr in (0.05, 0.10, 0.15, 0.20, 0.25):
        if thr < confirm:
            print(f"    confirm={thr:.2f}: {sum(1 for x in lives if x >= thr):>3} would fire")
    print(f"  → these edges are overwhelmingly fleeting; {confirm}s filters the tail without "
          f"clipping near-viable edges (re-visit only on a tiny-live fill-round-trip measurement).")


def fill_outcomes() -> None:
    """§C — the verdict's LEADING indicator from fill_success.csv: outcome mix, which leg legs out,
    the detection-quality (ticker-phantom) fraction, and progress toward the settled-both-fill floor.
    A `kalshi_moved` row with `kalshi_fillable==0` is the Kalshi REST orderbook showing NO fillable
    size at the limit while the ticker-detected price implied an edge — a phantom, not a fill-race
    loss (a sub-ms window proves it: nothing decays in ~1ms). The measurement is verified (the read
    is a mode-independent REST get_orderbook with positive controls; see verdict_methodology.md).
    MECHANISM (probed 2026-06-24): two probes — kalshi_quote_backing (REST quote, 0/120) AND
    kalshi_ws_ticker_backing (WS ticker channel, 0/34) — show random live markets are backed by DEEP
    books, so thin-book AND general-stale-feed are both REFUTED. Leading read: the detector SELECTS the
    moments the ticker is momentarily stale-low (that's what manufactures the apparent edge), so phantoms
    cluster in the DETECTED subset while random markets look fine. See PROJECT_STATE / TODO (the lever is
    orderbook-sourced Kalshi detection)."""
    if not _FILL_CSV.exists():
        print(f"\n§C fill outcomes: {_FILL_CSV} not found — skipping.")
        return
    rows = list(csv.DictReader(_FILL_CSV.open()))
    n = len(rows)
    print(f"\n§C  Fill outcomes (the verdict's leading indicator) — {n} rows "
          f"({'PROVISIONAL — small N' if n < 100 else 'N≥100'})")
    if not n:
        return

    # regime
    ts = sorted(r.get("timestamp", "") for r in rows if r.get("timestamp"))
    pre = sum(1 for t in ts if t < _R2_CUTOVER)
    print(f"  window: {ts[0]} → {ts[-1]}   "
          + (f"⚠️ {pre} row(s) PRE-cutover (mixed regime)" if pre else "all post-cutover (R2-clean)"))

    # outcome mix + both-fill rate
    from collections import Counter
    mix = Counter(r["outcome"] for r in rows)
    print("  outcome mix:", {k: mix[k] for k in sorted(mix)})
    bf = mix.get("both_fill", 0)
    print(f"  both-fill rate: {bf}/{n} = {100*bf/n:.0f}%  "
          f"— NOT good/bad on its own (verdict_methodology.md), and CONFOUNDED by the ticker-phantom below")

    # fill_window_ms by outcome (latency-vs-detection-quality tell)
    print("  fill_window_ms by outcome (a sub-ms loss can't be a race → detection-quality, not latency):")
    by_out: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if (v := _num(r, "fill_window_ms")) is not None:
            by_out[r["outcome"]].append(v)
    for out in sorted(by_out):
        v = by_out[out]
        print(f"    {out:>12}: n={len(v):>2}  median {median(v):>7.1f}  min {min(v):>6.1f}  max {max(v):>7.1f}")

    # detection-quality (ticker-phantom) fraction of kalshi_moved, segmented by side
    km = [r for r in rows if r["outcome"] == "kalshi_moved"]
    phantom = [r for r in km if _num(r, "kalshi_fillable") == 0]
    print(f"  detection-quality fraction: {len(phantom)}/{len(km)} kalshi_moved rows have "
          f"kalshi_fillable=0 (ticker-phantom signature)" + (f" = {100*len(phantom)//len(km)}%" if km else ""))
    side = Counter(r["kalshi_side"] for r in km)
    side_ph = Counter(r["kalshi_side"] for r in phantom)
    for s in sorted(side):
        print(f"    side={s}: {side_ph.get(s,0)}/{side[s]} phantom  "
              f"(WATCH: does the phantom over-detection concentrate on one side as N grows?)")

    # settled-both-fill progress (the verdict denominator) — computed via the SHARED
    # verdict_counts so this §C and scripts.data_audit §1 cannot drift. SETTLED only (pending is
    # not settled), frozen-book both_fills excluded, counted at GAME granularity for the floor.
    settled_keys = (settled_triple_keys(list(csv.DictReader(_CAPTURE_CSV.open())))
                    if _CAPTURE_CSV.exists() else set())
    both = [r for r in rows if r["outcome"] == "both_fill"]
    vc = verdict_counts(both, settled_keys)
    floor = config.SETTLEMENT_MIN_HEDGED_N
    ns_g, ns_t = vc["n_settled_games"], vc["n_settled_triples"]
    print(f"  settled∩both_fill (the E[settle|both_fill] denominator, GAME-keyed for the floor): "
          f"{ns_g}/{floor} games settled ({ns_t} triples / {vc['n_games']} fresh games / "
          f"{vc['n_triples']} fresh triples) — "
          f"{'essentially no conditional data yet' if ns_g < floor else 'floor reached'}.")
    print(f"  frozen-book exclusion (age > {config.FROZEN_BOOK_AGE_S:.0f}s = stale phantom, not counted): "
          f"{vc['n_frozen_rows']} frozen + {vc['n_unknown_rows']} age-unknown of {vc['n_both_rows']} "
          f"both_fill rows excluded; {vc['n_fresh_rows']} fresh remain.")
    if vc.get("n_freeze_excluded_rows"):
        print(f"  freeze-episode exclusion (feed-wide Poly freeze window dropped WHOLESALE — freshness "
              f"untrustworthy, incl. fresh-reading recovery phantoms): {vc['n_freeze_excluded_rows']} "
              f"both_fill rows excluded (see docs/data_regimes.md).")
    print("  caveat: fill_window_ms is an OPTIMISTIC proxy (omits the ~30ms order RTT); the real "
          "both-fill rate is lower. Do NOT read the low rate as 'uncapturable' while the phantom confound stands.")


def main() -> None:
    decay_curve()
    not_persistent_lifetimes()
    fill_outcomes()


if __name__ == "__main__":
    main()
