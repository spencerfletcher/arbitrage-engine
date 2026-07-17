"""
scripts/settlement_backtest.py — pre-committed falsification gate
─────────────────────────────────────────────────────────────────
Offline test of recorded would_fire rows against the strategy's own pessimistic model,
to FALSIFY (cheaply, before capital) the thesis that real cross-venue edges survive once
fills, fees, and settlement tails are priced honestly.

AUTHORITY: outputs exactly one of `KILL`, `PROVISIONAL PASS — TINY-LIVE ONLY`, or
`INSUFFICIENT DATA → DO NOT DEPLOY`. It can NEVER output "deploy." Deploy authority is a
separate runtime check (Gate 3, in safety.py/sizing) — not implemented here.

Conservatism is deliberate; do not soften. If a REQUIRED field is missing, this RAISES —
never defaults to 0 (a silent-zero fee/flatten/tail is the exact failure this prevents).

Inputs: logs/would_fire.csv (+ logs/would_fire_samples.csv with the later Poly bid).
Run:  .venv/bin/python -m scripts.settlement_backtest
"""
from __future__ import annotations

import csv
import math
import os
import sys
from collections import Counter, defaultdict

# Bootstrap repo root onto sys.path so `bot.*` imports resolve when this script is
# run directly (python scripts/settlement_backtest.py) — without it, only scripts/
# is on the path. Same pattern as scripts/verify_connections.py.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core import config
from bot.kalshi.cross_arb import _effective_share_cost, _kalshi_taker_fee

_WF = "logs/would_fire.csv"
_WFS = "logs/would_fire_samples.csv"
_CAPTURE = "logs/settlement_capture.csv"   # empirical settlement outcomes (scripts.settlement_capture)
_POLY_THETA = 0.05            # Poly US taker θ (official)
_RTT_OFFSET = 0.5            # sample offset (s) proxying the unwind / Kalshi-arrival moment.
# Grounded 2026-06-24 from the sub-second decay curve (scripts/subsecond_calibration.py): the
# unwind fills ~2 order RTTs + a sell-back after detection (~0.25–0.5s), where the bid is ~5.5¢
# off t0 — vs the old assumed 1.0s, which sat past the decay knee (~6.5¢) and over-charged
# flatten cost. 0.5 is the conservative end of that window (the unwind-fill latency is itself
# DRY-unmeasured, so don't model it as instant at 0.25). PROVISIONAL (N=9 wf_ids) — re-derive
# via subsecond_calibration §A as the clean-regime data grows.
_STALE_DROP = 0.005         # edge drop (abs) within 1–3s that flags a row "stale"

# Structural constants (lists; the scalar knobs live in config and are env-tunable).
F_SWEEP = [config.SETTLEMENT_F_PASS, 0.5, 0.7]
TAIL_SENSITIVITY = [1, 2]


def _load_wf() -> list[dict]:
    if not os.path.exists(_WF):
        return []
    with open(_WF, newline="") as f:
        return list(csv.DictReader(f))


def _empirical_tail_rates() -> tuple[float, float, int] | None:
    """Pooled (P_VOID, P_DIV, n_settled_games) from logs/settlement_capture.csv, or None
    when absent/empty. These GROUND the placeholder void/divergence rates with observed
    settlements. Pooled across series for the verdict; the per-series breakdown is printed
    separately (and is gross-failure detection, not validation — see settlement_capture)."""
    if not os.path.exists(_CAPTURE):
        return None
    with open(_CAPTURE, newline="") as f:
        rows = list(csv.DictReader(f))
    settled = [r for r in rows if r.get("outcome") in ("clean", "void", "divergence")]
    n = len(settled)
    if n == 0:
        return None
    n_void = sum(1 for r in settled if r["outcome"] == "void")
    n_div = sum(1 for r in settled if r["outcome"] == "divergence")
    return n_void / n, n_div / n, n


def _load_samples() -> dict[str, list[dict]]:
    by_id: dict[str, list[dict]] = defaultdict(list)
    if os.path.exists(_WFS):
        with open(_WFS, newline="") as f:
            for r in csv.DictReader(f):
                by_id[r["wf_id"]].append(r)
        for rows in by_id.values():
            rows.sort(key=lambda r: float(r.get("offset_s") or 0))
    return by_id


def _req(row: dict, key: str, ctx: str) -> str:
    """Return row[key] or RAISE — required fields must never silently default."""
    v = row.get(key, "")
    if v in ("", None):
        raise ValueError(
            f"settlement_backtest: required field '{key}' missing/empty in {ctx}. "
            f"Refusing to default to 0 (silent-zero is the failure this gate prevents). "
            f"If this is the sampler's poly_bid, the rows predate the bid-logging fix — "
            f"only rows logged after it are usable."
        )
    return v


def _num(row: dict, key: str):
    v = row.get(key, "")
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _poly_fee(s: float, p: float) -> float:
    return _POLY_THETA * s * p * (1.0 - p)                 # Poly US taker, θ=0.05


def _kalshi_fee_total(s: int, p: float) -> float:
    return math.ceil(0.07 * s * p * (1.0 - p) * 100) / 100  # Kalshi taker, roundup to cent


def _implied_edge(poly_ask: float, kalshi_ask: float) -> float:
    return 1.0 - _effective_share_cost(poly_ask, _POLY_THETA) - (kalshi_ask + _kalshi_taker_fee(kalshi_ask))


# ── Frozen-feed (phantom-depth) detector ────────────────────────────────────────
# The bug this guards against: at the fire path the bot re-fetches Poly PRICE and STATE
# fresh from REST but reads Poly DEPTH from a cached WS book that can FREEZE during a live
# game (bot/runner/kalshi_arb.py:497-499). Kalshi depth is REST-sourced and authoritative;
# only Poly depth has this hole. A frozen cache logs stale PHANTOM depth (e.g. 726257 when
# the real book was 3). The backtest then sizes off the phantom AND — because a frozen book
# makes the edge GROW rather than collapse — the edge-decay staleness test (_build_record
# below) misclassifies the row as a clean win-on-merit. Net: a fabricated favorable row
# inflates Σ hedged_pnl and biases toward PROVISIONAL PASS. We flag and EXCLUDE such rows.
#
# FIELDS AVAILABLE PER SAMPLE OFFSET — confirmed against the sampler write path
# (bot/runner/kalshi_arb.py _sample_book_evolution) and the would_fire_samples.csv header:
#     poly_ask, poly_depth (WS), poly_bid, kalshi_ask, kalshi_fillable, rest_poly_depth (fresh REST)
# PRIMARY signal: WS vs REST depth. `rest_poly_depth` is a same-offset, cache-busted REST read of
# the SAME quantity as the WS `poly_depth` (best-level ask depth) — an INDEPENDENT transport. WS
# holding byte-identical while REST moves (or disagreeing by an order of magnitude) is a frozen
# cache, with no dependence on price ticking. This is the ground truth the project's "stable ≠
# frozen" rule demands. FALLBACK signal (pre-fix / non-US rows lacking rest_poly_depth): PRICE
# MOVEMENT (Poly or Kalshi) as a flow proxy — real depth shouldn't hold byte-identical across a
# window in which prices moved. The proxy's blind spots (a freeze with no price tick; a stable
# book whose price ticks) are why the REST signal supersedes it whenever rest_poly_depth exists.
# A per-level traded-volume / book-sequence field would still make even the fallback exact.

def _series(samples: list[dict], key: str) -> list[float]:
    """Parsed numeric values for `key` across samples (blank/garbage skipped)."""
    return [v for sm in samples if (v := _num(sm, key)) is not None]


def _varies(vals: list[float], ndigits: int) -> bool:
    """True if `vals` holds >1 distinct value at the given rounding (i.e. it moved)."""
    return len({round(v, ndigits) for v in vals}) > 1


_DEPTH_PHANTOM_RATIO = 10.0   # >1 order of magnitude apart
_DEPTH_ABS_FLOOR     = 50.0   # shares; below this, depth gaps are thin-book jitter, not a phantom


def _depth_disagrees(ws: float, rest: float) -> bool:
    """True iff WS best-level depth and the fresh-REST best-level depth disagree by more than
    cross-transport jitter can explain — an order-of-magnitude phantom (726257 vs 3), not the
    few-share delta of two transports read ms apart. Ratio-based (jitter is multiplicative, the
    phantom is ~242,000×); tolerant near zero (both small → noise, never a fabricated phantom)."""
    hi, lo = max(ws, rest), min(ws, rest)
    if hi <= _DEPTH_ABS_FLOOR:
        return False
    return hi > _DEPTH_PHANTOM_RATIO * max(lo, 1.0)


def _poly_depth_suspect(samples: list[dict]) -> tuple[bool, str | None]:
    """Flag a row whose logged Poly WS depth is a frozen-cache phantom. Returns (suspect, reason).

    PRIMARY signal (when ≥2 samples carry `rest_poly_depth`): WS-vs-REST cross-transport
    ground truth — the WS cache vs a same-offset, cache-busted REST read of the *same* quantity
    (best-level ask depth). An independent transport, so it doesn't need price to move:
      • 'ws-frozen-rest-moved' — WS depth held byte-identical while the fresh REST book MOVED.
        DEFINITIVE freeze (no tolerance judgment); catches freezes during no-price-tick windows.
      • 'ws-rest-divergence'   — WS flat AND REST flat but the two magnitudes disagree by >1 order
        of magnitude (726257 vs 3). Carries the `_depth_disagrees` tolerance, so it reads as
        "phantom OR thin-book-moving-fast" — trust it as an UPPER BOUND on phantoms, not a
        certainty (the ambiguous band is ~60–500 shares at ~10×). Tuned conservative-on-catch:
        a false negative poisons the verdict; a false positive only shrinks N.
    WS flat + REST flat + magnitudes AGREE → genuinely stable book → NOT suspect (this is the
    case the old price-movement heuristic false-positived as 'partial-freeze').

    FALLBACK (no usable REST — pre-fix / non-US rows): the original price-movement heuristic,
    which uses Poly/Kalshi price ticks as a PROXY for flow:
      • 'partial-freeze' — Poly's own ask/bid ticked but depth never moved.
      • 'total-freeze'   — Poly ask+bid+depth all flat while Kalshi moved (the Mexico case).
    LIMITATION (fallback only): with no REST column and no volume-through-level / book-sequence
    field, a freeze during a window where neither displayed price ticked is MISSED, and a
    stable-depth book whose price ticks is wrongly flagged. The REST path above resolves both
    when `rest_poly_depth` is present (it is, on all post-2026-06-22 US rows).

    ⚠️ `rest_poly_depth` CHANGED MEANING 2026-07-15, and it changed this function's reach. It used
    to log blank for a genuinely-empty ask book, and `_series` drops blanks — so a WS phantom
    against a REALLY empty book (the widest divergence there is) lost its REST samples and fell
    through to the weaker fallback below. It now logs 0 for a book we read and found empty, blank
    only for one we never read, so those rows reach 'ws-rest-divergence' where they belong.
    Rows in logs/rotated/would_fire_samples.pre-biddepth.csv and earlier carry the old meaning
    (blank = empty OR unread); this only ever ADDS samples, never reinterprets old ones."""
    depths = _series(samples, "poly_depth")
    if len(depths) < 2:
        return False, None                       # not enough WS samples to judge

    rest = _series(samples, "rest_poly_depth")
    if len(rest) >= 2:                            # ── REST-PRIMARY: cross-transport ground truth
        ws_flat = not _varies(depths, 6)
        if ws_flat and _varies(rest, 6):
            return True, "ws-frozen-rest-moved"  # WS stale, real book moved → definitive freeze
        if ws_flat and not _varies(rest, 6):
            # Pair WS vs REST WITHIN a row (not positional zip — _series drops blanks per key,
            # so the two lists can desync). _num → None for a blank, never 0 (blank ≠ zero).
            for sm in samples:
                w, r = _num(sm, "poly_depth"), _num(sm, "rest_poly_depth")
                if w is not None and r is not None and _depth_disagrees(w, r):
                    return True, "ws-rest-divergence"   # static phantom (726257 vs 3)
            return False, None                   # WS flat + REST flat + agree → genuinely stable
        return False, None                       # WS depth varied → live book consuming flow

    # ── FALLBACK: price-movement heuristic (no usable REST column) ──────────────────────────
    if _varies(depths, 6):
        return False, None                       # depth changed → live book
    if max(depths) <= 0:
        return False, None                       # 0-depth is the thin-depth case, not a freeze
    if _varies(_series(samples, "poly_ask"), 4) or _varies(_series(samples, "poly_bid"), 4):
        return True, "partial-freeze"            # Poly price moved, depth frozen → phantom
    if _varies(_series(samples, "kalshi_ask"), 4):
        return True, "total-freeze"              # Poly fully flat while Kalshi moved → frozen
    return False, None                           # nothing moved → cannot distinguish; don't flag


def _build_record(wf: dict, samples: list[dict]) -> dict:
    """One per-row record; all gates read it. RAISEs on any missing required field."""
    wid = wf.get("wf_id", "?")
    ctx = f"would_fire wf_id={wid}"
    # s = min fillable across both legs, capped by detection size. Both legs are
    # REST fillable-at-limit (would_fire.csv: poly_fillable + kalshi_fillable).
    shares = int(float(_req(wf, "shares", ctx)))
    poly_fillable = float(_req(wf, "poly_fillable", ctx))
    kalshi_fillable = float(_req(wf, "kalshi_fillable", ctx))
    s = max(0, min(shares, int(poly_fillable), int(kalshi_fillable)))
    pp = float(_req(wf, "poly_limit", ctx))      # price-capped Poly limit
    kp = float(_req(wf, "kalshi_limit", ctx))    # price-capped Kalshi limit

    # poly_bid_later: the bid an unwind sells into ~1 RTT later (flatten cost). REQUIRED.
    bid_sample = None
    if samples:
        ordered = sorted(samples, key=lambda x: abs(float(x.get("offset_s") or 0) - _RTT_OFFSET))
        for sm in ordered:
            if _num(sm, "poly_bid") is not None:
                bid_sample = sm
                break
    if bid_sample is None:
        raise ValueError(
            f"settlement_backtest: no sampler poly_bid for {ctx} — flatten cost is "
            f"uncomputable. Rows must be logged after the poly_bid sampler fix; "
            f"pre-fix rows cannot be scored (do not default to 0)."
        )
    poly_bid_later = _num(bid_sample, "poly_bid")

    hedged_pnl = s * (1.0 - pp - kp) - _poly_fee(s, pp) - _kalshi_fee_total(s, kp)
    # Flatten: bought Poly @pp, sell into the later bid; pay Poly taker on both sides.
    flatten_cost = (s * (pp - poly_bid_later)
                    + _poly_fee(s, pp) + _poly_fee(s, poly_bid_later))
    stake_row = s * (pp + kp)

    # Staleness: edge collapsed within 1–3s (you win these races *because* they're stale).
    edge0 = _implied_edge(float(_req(wf, "poly_ask_raw", ctx)), float(_req(wf, "kalshi_ask", ctx)))
    later_edges = [
        _implied_edge(_num(sm, "poly_ask"), _num(sm, "kalshi_ask"))
        for sm in samples
        if float(sm.get("offset_s") or 0) > 0
        and _num(sm, "poly_ask") is not None and _num(sm, "kalshi_ask") is not None
    ]
    stale = bool(later_edges) and (min(later_edges) < edge0 - _STALE_DROP)

    # naive-hedged = both legs ≥s fillable-at-limit at fire (Poly + Kalshi), the record.
    naive_hedged = (poly_fillable >= s and kalshi_fillable >= s and s >= 1)

    # Frozen-feed guard: is the logged Poly depth a phantom from a frozen WS cache? (A
    # suspect row can still be naive_hedged — the phantom depth is exactly what makes it
    # look hedgeable — so this is a SEPARATE flag, applied as an exclusion downstream.)
    poly_depth_suspect, suspect_reason = _poly_depth_suspect(samples)

    return {
        "wf_id": wid, "s": s, "pp": pp, "kp": kp,
        "hedged_pnl": hedged_pnl, "flatten_cost": max(0.0, flatten_cost),
        "stake_row": stake_row, "stale": stale, "naive_hedged": naive_hedged,
        "poly_fillable": poly_fillable,
        "poly_depth_suspect": poly_depth_suspect, "suspect_reason": suspect_reason,
        "loss_given_div": 1.0 * stake_row,      # adverse divergence zeroes position; windfall priced $0
        "loss_given_void": s * config.SETTLEMENT_W_VOID,  # per-share LFMP↔fair wedge; ASSUMPTION
    }


def _tails_total(H: list[dict], scale: float) -> float:
    pv = config.SETTLEMENT_P_VOID * scale
    pd = config.SETTLEMENT_P_DIV * scale
    return sum(pv * r["loss_given_void"] + pd * r["loss_given_div"] for r in H)


def _won_set(H: list[dict], f: float) -> tuple[list[dict], str]:
    """W_f = stale rows first (lowest hedged_pnl), until round(f·|H|). You win the stale
    races, so the won-set is the WORST rows — never favorable/random."""
    want = round(f * len(H))
    stale = sorted((r for r in H if r["stale"]), key=lambda r: r["hedged_pnl"])
    if len(stale) >= want:
        return stale[:want], "PRICED"
    fresh = sorted((r for r in H if not r["stale"]), key=lambda r: r["hedged_pnl"])
    won = stale + fresh[: want - len(stale)]
    status = "UNPRICED (count-only)" if not stale else "PARTIALLY PRICED"
    return won, status


def _net(H: list[dict], f: float, tail_scale: float) -> tuple[float, float, float, float]:
    """Return (net_after_tails, sum_won_pnl, sum_flatten, net_before_tails)."""
    won, _ = _won_set(H, f)
    won_ids = {id(r) for r in won}
    sum_won = sum(r["hedged_pnl"] for r in won)
    sum_flat = sum(r["flatten_cost"] for r in H if id(r) not in won_ids)
    before = sum_won - sum_flat
    return before - _tails_total(H, tail_scale), sum_won, sum_flat, before


def _breakeven(H: list[dict], param: str) -> float | None:
    """Value of P_DIV or W_VOID at which net(F_PASS) crosses 0 (else None)."""
    fp = config.SETTLEMENT_F_PASS
    won, _ = _won_set(H, fp)
    won_ids = {id(r) for r in won}
    before = sum(r["hedged_pnl"] for r in won) - sum(r["flatten_cost"] for r in H if id(r) not in won_ids)
    if param == "P_DIV":
        unit = sum(r["loss_given_div"] for r in H)
        other = config.SETTLEMENT_P_VOID * sum(r["loss_given_void"] for r in H)
    else:  # W_VOID (per-share); divergence term held fixed
        unit = config.SETTLEMENT_P_VOID * sum(r["s"] for r in H)
        other = config.SETTLEMENT_P_DIV * sum(r["loss_given_div"] for r in H)
    if unit <= 0:
        return None
    return (before - other) / unit


def _evaluate(H: list[dict]) -> dict:
    """Pure gate evaluation over the hedged-record set H. Returns the verdict + the
    numbers the report prints. Separated from I/O so each verdict path is unit-testable."""
    fp = config.SETTLEMENT_F_PASS
    if 0.3 * len(H) < config.SETTLEMENT_MIN_HEDGED_N:
        return {"verdict": "INSUFFICIENT DATA", "H": len(H),
                "gap": config.SETTLEMENT_MIN_HEDGED_N - 0.3 * len(H)}

    nets, quality = {}, None
    for f in F_SWEEP:
        _, q = _won_set(H, f)
        quality = quality or q
        n1, won, flat, before = _net(H, f, TAIL_SENSITIVITY[0])
        n2, _, _, _ = _net(H, f, TAIL_SENSITIVITY[1])
        nets[f] = {"won": won, "flat": flat, "before": before, "n1": n1, "n2": n2}

    pass_1x = nets[fp]["n1"] > 0
    fill_race = (not pass_1x) and any(nets[f]["n1"] > 0 for f in F_SWEEP if f >= 0.7)
    if not pass_1x:
        verdict = "KILL: fill-race artifact" if fill_race else "KILL"
    elif nets[fp]["n2"] <= 0:
        verdict = "PROVISIONAL PASS (BALANCED ON TAIL ASSUMPTION)"
    else:
        verdict = "PROVISIONAL PASS"
    return {"verdict": verdict, "H": len(H), "nets": nets, "quality": quality,
            "be_pdiv": _breakeven(H, "P_DIV"), "be_wvoid": _breakeven(H, "W_VOID")}


def _partition(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split naive-hedged records into the trustworthy set H and the frozen-feed
    exclusions. Suspect rows LEAVE H but are returned so the report can count them (never
    silently dropped) and so they can be held against the Gate-0 floor."""
    hedged = [r for r in records if r["naive_hedged"]]
    H = [r for r in hedged if not r["poly_depth_suspect"]]
    excluded = [r for r in hedged if r["poly_depth_suspect"]]
    return H, excluded


def _exclusions_caused_insufficient(n_H: int, n_excluded: int) -> bool:
    """True iff 0.3·|H| clears the MIN_HEDGED_N floor only when the excluded suspect rows
    are added back — i.e. the frozen-feed exclusions are what tipped it to INSUFFICIENT.
    (Pre-committed behavior: exclusions count AGAINST the floor; this never lowers it.)"""
    below = 0.3 * n_H < config.SETTLEMENT_MIN_HEDGED_N
    would_clear = 0.3 * (n_H + n_excluded) >= config.SETTLEMENT_MIN_HEDGED_N
    return below and would_clear


def _census(records: list[dict]) -> dict:
    """Frozen-feed census over ALL scorable rows: suspect count, reason mix, and — critically
    — whether suspects cluster in the high-hedged_pnl tail. If phantoms are disproportionately
    the fat-win rows, even the surviving sample is upward-biased, which should lower confidence
    in any near-term PROVISIONAL PASS."""
    suspect = [r for r in records if r["poly_depth_suspect"]]
    clean = [r for r in records if not r["poly_depth_suspect"]]
    mean = lambda rs: (sum(r["hedged_pnl"] for r in rs) / len(rs)) if rs else None
    sus_mean, cln_mean = mean(suspect), mean(clean)
    clustered = bool(suspect and clean and sus_mean > cln_mean)
    return {
        "total": len(records), "n_suspect": len(suspect), "n_clean": len(clean),
        "by_reason": dict(Counter(r["suspect_reason"] for r in suspect)),
        "suspect_mean_pnl": sus_mean, "clean_mean_pnl": cln_mean,
        "clustered_high_pnl": clustered,
        "suspect_keys": [(r["wf_id"], r["s"], r.get("poly_fillable"),
                          round(r["hedged_pnl"], 2), r["suspect_reason"]) for r in suspect],
    }


def main(census_only: bool = False) -> None:
    wf_rows = _load_wf()
    samples = _load_samples()
    if not wf_rows:
        print(f"No would_fire rows ({_WF} empty). Keep running DRY.")
        return

    records, errs = [], []
    for wf in wf_rows:
        try:
            records.append(_build_record(wf, samples.get(wf.get("wf_id", ""), [])))
        except ValueError as e:
            errs.append(str(e))
    if errs:
        # Surface the FIRST blocking error loudly — do not silently skip-and-pass.
        print(f"⚠️  {len(errs)} row(s) unscorable (required field missing). First:\n   {errs[0]}\n")

    H, excluded = _partition(records)
    cen = _census(records)

    # ── Frozen-feed census — read BEFORE trusting any verdict ──────────────────────
    print("══ frozen-feed census (poly_depth_suspect) ══")
    print(f"scorable rows: {cen['total']}   suspect: {cen['n_suspect']}   clean: {cen['n_clean']}")
    if cen["by_reason"]:
        print(f"  reasons: {cen['by_reason']}")
    if cen["n_suspect"]:
        cm = (f"{cen['clean_mean_pnl']:+.3f}" if cen["clean_mean_pnl"] is not None else "n/a")
        print(f"  mean hedged_pnl  suspect={cen['suspect_mean_pnl']:+.3f}   clean={cm}")
        for wid, s, pd, pnl, reason in cen["suspect_keys"]:
            pd_str = f"{pd:.0f}" if isinstance(pd, (int, float)) else "?"
            print(f"    EXCLUDED wf_id={wid} s={s} poly_fillable={pd_str} "
                  f"hedged_pnl={pnl:+.2f} reason={reason}")
        if cen["clustered_high_pnl"]:
            print("  ⚠️ suspect rows skew to HIGHER hedged_pnl than clean — phantoms cluster in "
                  "the fat-win tail. Even the surviving sample may be upward-biased; LOWER "
                  "confidence in any near-term PROVISIONAL PASS.")
    print()

    if census_only:
        return

    # ── Empirical tail rates (ground the placeholders) + equivalence audit ─────────
    # The verdict below scores the FIREABLE would_fire subset ONLY — never the detected-edge
    # population (whose mean edge is upward-biased; the fillable/persistent survivors are
    # systematically lower-edge). This panel just swaps ASSUMED P_VOID/P_DIV for OBSERVED
    # ones when settlement_capture.csv exists; it is a sensitivity input, not a profit verdict.
    emp = _empirical_tail_rates()
    if emp is not None:
        pv, pdv, n = emp
        print(f"══ empirical tail rates from {_CAPTURE} ({n} settled games) ══")
        print(f"  P_VOID {config.SETTLEMENT_P_VOID} → {pv:.4f}   "
              f"P_DIV {config.SETTLEMENT_P_DIV} → {pdv:.4f}   (overriding placeholders for the verdict)")
        config.SETTLEMENT_P_VOID, config.SETTLEMENT_P_DIV = pv, pdv
        try:
            from scripts.settlement_capture import audit as _capture_audit, _load_existing_records, _print_audit
            _print_audit(_capture_audit(_load_existing_records()))
        except Exception as exc:                       # audit is advisory; never block the verdict
            print(f"  (audit unavailable: {exc!r})")
        print()
    else:
        print(f"══ tail rates: PLACEHOLDERS (no {_CAPTURE} yet — run scripts.settlement_capture) ══")
        print(f"  P_VOID={config.SETTLEMENT_P_VOID}  P_DIV={config.SETTLEMENT_P_DIV} (assumed)\n")

    r = _evaluate(H)
    print("══ Pre-committed falsification gate (KILL / PROVISIONAL / INSUFFICIENT only) ══")
    print(f"|H| naive-hedged & trusted : {len(H)}   0.3·|H| = {0.3*len(H):.1f}   "
          f"(need ≥ {config.SETTLEMENT_MIN_HEDGED_N})")
    if excluded:
        print(f"  {len(excluded)} naive-hedged row(s) EXCLUDED as frozen-feed phantom depth "
              f"(see census) — these count AGAINST the floor, never toward it.")
    if r["verdict"] == "INSUFFICIENT DATA":
        if _exclusions_caused_insufficient(len(H), len(excluded)):
            print("  → frozen-feed exclusions CAUSED this verdict: without them 0.3·|H| would "
                  "clear the floor. Correct outcome — those rows were not trustworthy, so the "
                  "real sample is thinner than it looked. Do NOT lower the floor to compensate.")
        elif excluded:
            print(f"  → would be INSUFFICIENT regardless; exclusions removed {len(excluded)} more.")
        print(f"\nVERDICT: INSUFFICIENT DATA → DO NOT DEPLOY (short {r['gap']:.1f} "
              f"hedged-equivalent). Valid outcome — keep running DRY.")
        return

    print("\nfill-sweep:  f |   Σ won_pnl |  Σ flatten | net_pre_tails | net_1x | net_2x")
    for f in F_SWEEP:
        n = r["nets"][f]
        print(f"           {f:.1f} | {n['won']:+10.2f} | {n['flat']:10.2f} | "
              f"{n['before']:+12.2f} | {n['n1']:+6.2f} | {n['n2']:+6.2f}")
    print("\nbreak-even P_DIV  : "
          + (f"{r['be_pdiv']:.4f}  (assumed {config.SETTLEMENT_P_DIV})" if r["be_pdiv"] is not None else "n/a"))
    print("break-even W_VOID : "
          + (f"{r['be_wvoid']:.4f}  (assumed {config.SETTLEMENT_W_VOID}) ← least-grounded"
             if r["be_wvoid"] is not None else "n/a"))
    print(f"win-quality       : {r['quality']}")
    print("\nstamped assumptions (PLACEHOLDERS — ground from tournament history before trusting a PASS):")
    print(f"  P_VOID={config.SETTLEMENT_P_VOID}  P_DIV={config.SETTLEMENT_P_DIV}  "
          f"W_VOID={config.SETTLEMENT_W_VOID} (ASSUMPTION, unobserved)")
    print("  void/divergence rates ASSUMED, not validated against observed settlements "
          "→ verdict capped at PROVISIONAL-with-void-untested.")
    print(f"\nVERDICT: {r['verdict']} — TINY-LIVE ONLY (void-untested)."
          if r["verdict"].startswith("PROVISIONAL") else f"\nVERDICT: {r['verdict']}.")


if __name__ == "__main__":
    # `--census` prints only the frozen-feed census (run before trusting any verdict).
    main(census_only=("--census" in sys.argv))
