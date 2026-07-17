"""
scripts/settlement_capture.py — capture-forward settlement of detected/would-fire edges
────────────────────────────────────────────────────────────────────────────────────────
The highest-value pre-live deliverable: take the edges we DETECTED (would_fire.csv when it
has rows, else the rejected-edge ledger which carries the same poly_token/kalshi_ticker),
look up how each game ACTUALLY settled on both venues, and classify it
clean / void / divergence (bot.kalshi.settlement). Two outputs:

  1. An empirical per-allowlist-entry equivalence AUDIT — `series: N games, clean/void/diverge`
     — the first real check that _SETTLEMENT_EQUIVALENT (matcher.py:56) holds against
     outcomes, not just a doc read. It is GROSS-FAILURE DETECTION (catch a divergence), never
     positive validation from a small clean sample (esp. KXWCGAME — see the printed caveat).
  2. Empirical void/divergence rates to ground SETTLEMENT_P_VOID / SETTLEMENT_P_DIV.

Writes ONLY logs/settlement_capture.csv. It is HARD-BARRED from writing
logs/execution_pnl.csv — that file feeds safety.cumulative_realized_loss (the live loss cap);
a hypothetical/capture-forward loss leaking into it is the blind-to-losses bug inverted.

Read-only against the venues (settlement + market GETs). Run:
  .venv/bin/python -m scripts.settlement_capture
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.kalshi.cross_arb import _effective_share_cost, _kalshi_taker_fee
from bot.kalshi.settlement import Outcome, classify, realized_pnl  # noqa: F401 (classify via fetch)

_OUT = "logs/settlement_capture.csv"
_EXEC_PNL = "logs/execution_pnl.csv"   # the live loss-cap file — capture must NEVER write here
_WF = "logs/would_fire.csv"
_REJ = "logs/rejected_edges.csv"
_FS = "logs/fill_success.csv"           # the actually-FILLED both_fill hedges (verdict numerator)
_POLY_THETA = 0.05                      # Poly US taker θ (matches settlement_backtest)

_HEADER = [
    "timestamp", "source", "event", "series", "poly_token", "kalshi_ticker", "kalshi_side",
    "outcome", "poly_settlement_price", "kalshi_result", "poly_payout", "kalshi_payout",
    "shares", "poly_eff", "kalshi_eff", "realized_pnl", "detail",
]


def assert_safe_output(path: str) -> None:
    """Refuse to write the live loss-cap file. A hard assertion, not a convention:
    capture-forward records HYPOTHETICAL settlements; routing one into execution_pnl.csv
    would feed a fake loss into safety.cumulative_realized_loss (the inverted
    blind-to-losses bug). Raises ValueError on any path resolving to execution_pnl.csv."""
    if os.path.basename(os.path.normpath(path)) == os.path.basename(_EXEC_PNL):
        raise ValueError(
            f"settlement_capture refuses to write '{path}': that is the live loss-cap file "
            f"({_EXEC_PNL}). Capture-forward is hypothetical and must never feed the loss cap."
        )


def series_of(ticker: str) -> str:
    return (ticker or "").split("-", 1)[0]


def _num(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def load_candidates() -> list[dict]:
    """Distinct fireable directions to score, from would_fire.csv (preferred — carries
    fillable) then rejected_edges.csv (same token/ticker/side, detection-time prices).
    Deduped by (poly_token, kalshi_ticker, kalshi_side)."""
    cands: dict[tuple, dict] = {}

    def _add(source, event, poly_token, ticker, side, poly_ask, kalshi_ask, edge, shares):
        if not (poly_token and ticker and side):
            return
        key = (poly_token, ticker, side)
        if key in cands:
            return
        cands[key] = {
            "source": source, "event": event, "poly_token": poly_token,
            "kalshi_ticker": ticker, "kalshi_side": side, "poly_ask": _num(poly_ask),
            "kalshi_ask": _num(kalshi_ask), "edge": _num(edge), "shares": _num(shares),
        }

    if os.path.exists(_WF):
        with open(_WF, newline="") as f:
            for r in csv.DictReader(f):
                _add("would_fire", r.get("event"), r.get("poly_token"), r.get("kalshi_ticker"),
                     r.get("kalshi_side"), r.get("poly_ask_raw"), r.get("kalshi_ask"),
                     r.get("edge"), r.get("shares"))
    if os.path.exists(_REJ):
        with open(_REJ, newline="") as f:
            for r in csv.DictReader(f):
                _add("rejected", r.get("event"), r.get("poly_token"), r.get("kalshi_ticker"),
                     r.get("kalshi_side"), r.get("poly_ask"), r.get("kalshi_ask"),
                     r.get("edge"), r.get("shares"))
    # fill_success.csv carries the actually-FILLED both_fill hedges — the verdict numerator. The two
    # sources above usually cover them too (a both_fill token also churns as rejected edges for the
    # same game), but only INCIDENTALLY: a both_fill whose token never logged a reject, or whose
    # reject rows rotate out before the game settles, would be silently unscoreable. Adding both_fill
    # rows here makes every hedge a settlement candidate BY CONSTRUCTION. Lowest priority on purpose —
    # `_add` skips keys already present, so a both_fill also in would_fire/rejected keeps that richer
    # row; fill_success only fills genuine gaps. both_fill ONLY: single-leg/no-fill outcomes have no
    # hedge to settle.
    #
    # CONSEQUENCE (do not filter capture by source=='fill_success'): because every both_fill triple
    # ALSO churned as a detection-time would_fire/rejected edge, the fill_success branch effectively
    # ALWAYS loses the dedupe key → `source=='fill_success'` is structurally unreachable in
    # production (it stays 0 rows). The branch is a real gap-filler safety net, not dead code — it
    # just (correctly) never fires with current log retention. Isolate filled hedges by the TRIPLE
    # join against fill_success.csv, never by this source value. Pinned: test_settlement_capture.py
    # ::test_both_fill_in_all_three_sources_never_sourced_fill_success.
    #
    # ASK BASIS NOTE (known-not-surprising): poly_ask here is `live_poly_ask` (the fill-time ask) vs
    # detection-time in the other two sources. That asymmetry feeds ONLY `realized_pnl`, a
    # capture-forward SENSITIVITY figure (the _economic_events summary print) — NOT the verdict. The
    # verdict E[settle|both_fill] is settlement-OUTCOME based (clean/void/divergence + the
    # settled∩both_fill count), both ask-INDEPENDENT (subsecond_calibration §C, settlement_backtest
    # _empirical_tail_rates), so the cross-source basis mix cannot perturb the conditional.
    if os.path.exists(_FS):
        with open(_FS, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("outcome") != "both_fill":
                    continue
                _add("fill_success", r.get("event"), r.get("poly_token"), r.get("kalshi_ticker"),
                     r.get("kalshi_side"), r.get("live_poly_ask"), r.get("kalshi_ask"),
                     r.get("edge"), r.get("target_shares"))
    return list(cands.values())


def _eff_costs(poly_ask, kalshi_ask):
    """(poly_eff, kalshi_eff) per-share, or (None, None) if asks missing. Same convention
    as settlement_backtest (_POLY_THETA, Kalshi taker roundup)."""
    if poly_ask is None or kalshi_ask is None:
        return None, None
    return _effective_share_cost(poly_ask, _POLY_THETA), kalshi_ask + _kalshi_taker_fee(kalshi_ask)


def audit(records: list[dict]) -> dict:
    """Per-series equivalence audit over scored records. Counts DISTINCT GAMES (by
    kalshi_ticker) and their outcomes; reports clean/void/divergence/pending and the
    empirical void/divergence rate over SETTLED (non-pending) games. Pure — testable on
    synthetic records. Flags any game whose directions disagree on outcome (a real bug)."""
    by_series: dict[str, dict[str, str]] = defaultdict(dict)   # series -> {ticker: outcome}
    conflicts: list[str] = []
    for r in records:
        s = series_of(r["kalshi_ticker"])
        prior = by_series[s].get(r["kalshi_ticker"])
        oc = r["outcome"]
        if prior is None:
            by_series[s][r["kalshi_ticker"]] = oc
        elif prior != oc and Outcome.PENDING.value not in (prior, oc):
            conflicts.append(f"{r['kalshi_ticker']}: {prior} vs {oc}")

    out = {"by_series": {}, "conflicts": conflicts}
    for s, games in by_series.items():
        vals = list(games.values())
        settled = [v for v in vals if v != Outcome.PENDING.value]
        n_void = settled.count(Outcome.VOID.value)
        n_div = settled.count(Outcome.DIVERGENCE.value)
        n_clean = settled.count(Outcome.CLEAN.value)
        n_set = len(settled)
        out["by_series"][s] = {
            "games": len(vals), "settled": n_set, "clean": n_clean,
            "void": n_void, "diverge": n_div, "pending": len(vals) - n_set,
            "p_void": (n_void / n_set) if n_set else None,
            "p_div": (n_div / n_set) if n_set else None,
        }
    return out


def _print_audit(a: dict) -> None:
    print("\n══ settlement-equivalence audit (per allowlist entry / Kalshi series) ══")
    if not a["by_series"]:
        print("  (no scorable games)")
    for s, c in sorted(a["by_series"].items()):
        pv = f"{c['p_void']:.3f}" if c["p_void"] is not None else "n/a"
        pdv = f"{c['p_div']:.3f}" if c["p_div"] is not None else "n/a"
        print(f"  {s:10s}: {c['settled']} settled games "
              f"(clean={c['clean']} void={c['void']} diverge={c['diverge']}, "
              f"pending={c['pending']})   empirical P_void={pv} P_div={pdv}")
    if a["conflicts"]:
        print("  ⚠️ DIRECTION-DISAGREEMENT (same game, different outcome — investigate):")
        for cf in a["conflicts"]:
            print(f"      {cf}")
    print("\n  NOTE: this is GROSS-FAILURE DETECTION, not validation. Absence of divergence")
    print("  in a small sample does NOT bless an entry. KXWCGAME:drawable_outcome in")
    print("  particular needs MANY more soccer games regardless of what a handful show")
    print("  (group stage is finite, knockouts are off-allowlist) — 'never got enough WC")
    print("  games' is a valid terminal state for that entry, not a bar to lower. MLB")
    print("  reaches sufficient-N on daily volume.")


def _game_id(ticker: str) -> str:
    """Strip the team suffix so the two settlement-equivalent tickers for one game share a
    key: KXMLBGAME-26JUN201420TORCHC-CHC / -TOR → KXMLBGAME-26JUN201420TORCHC. Verified
    against all MLB+WC tickers in the logs — every one is SERIES-DATETEAMS-TEAM with no
    internal dash in the date segment, so rsplit strips only the team."""
    return (ticker or "").rsplit("-", 1)[0]


def _economic_events(records: list[dict]) -> tuple[int, int, float, int]:
    """Collapse settlement-equivalent ticker rows (two tickers, ONE economic hedge) to one
    event — REPORTING ONLY; the CSV and audit() are untouched. One event = rows sharing
    (event, poly_token, game_id) AND identical (poly_payout, kalshi_payout): the payouts are
    in the key on purpose, so a payout mismatch keeps rows SEPARATE and a real divergence can
    never be merged away. Returns (n_rows, n_events, deduped_settled_pnl, n_settled_events);
    deduped_settled_pnl sums ONE representative realized_pnl per SETTLED event (first
    occurrence — the equivalent rows differ only by bid-ask/fee and only one would execute)."""
    reps: dict[tuple, dict] = {}
    for r in records:
        key = (r.get("event"), r.get("poly_token"), _game_id(r.get("kalshi_ticker")),
               r.get("poly_payout", ""), r.get("kalshi_payout", ""))
        reps.setdefault(key, r)   # first occurrence is the representative
    settled = [r for r in reps.values() if r.get("outcome") != Outcome.PENDING.value]
    pnl = sum(_num(r.get("realized_pnl")) or 0.0 for r in settled)
    return len(records), len(reps), pnl, len(settled)


def _key(r: dict) -> tuple:
    return (r.get("poly_token"), r.get("kalshi_ticker"), r.get("kalshi_side"))


def _load_existing_records() -> list[dict]:
    if not os.path.exists(_OUT):
        return []
    with open(_OUT, newline="") as f:
        return list(csv.DictReader(f))


async def main() -> None:
    from bot.kalshi.client import KalshiClient
    from bot.poly_us.client import PolyUSClient
    from bot.kalshi.settlement import fetch_settlement

    assert_safe_output(_OUT)   # belt-and-suspenders before opening the writer
    cands = load_candidates()
    # Keep already-SETTLED rows (immutable); re-fetch PENDING + new directions (a game
    # pending today settles later). Full rewrite each run → no stale-PENDING duplicates.
    existing = _load_existing_records()
    settled_keys = {_key(r) for r in existing if r.get("outcome") != Outcome.PENDING.value}
    keep_rows = [r for r in existing if _key(r) in settled_keys]
    todo = [c for c in cands if (c["poly_token"], c["kalshi_ticker"], c["kalshi_side"]) not in settled_keys]
    print(f"candidates: {len(cands)}  already-settled: {len(settled_keys)}  to-fetch (pending+new): {len(todo)}")

    new_rows: list[list] = []
    if todo:
        poly = PolyUSClient()
        kalshi = KalshiClient()
        try:
            for c in todo:
                res = await fetch_settlement(
                    poly, kalshi, c["poly_token"], c["kalshi_ticker"], c["kalshi_side"]
                )
                p_eff, k_eff = _eff_costs(c["poly_ask"], c["kalshi_ask"])
                pnl = (realized_pnl(res, c["shares"], p_eff, k_eff)
                       if (c["shares"] and p_eff is not None) else None)
                new_rows.append([
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), c["source"], c["event"],
                    series_of(c["kalshi_ticker"]), c["poly_token"], c["kalshi_ticker"],
                    c["kalshi_side"], res.outcome.value,
                    "" if res.poly_settlement_price is None else f"{res.poly_settlement_price:.4f}",
                    res.kalshi_result,
                    "" if res.poly_payout is None else f"{res.poly_payout:.4f}",
                    "" if res.kalshi_payout is None else f"{res.kalshi_payout:.4f}",
                    "" if c["shares"] is None else f"{c['shares']:.0f}",
                    "" if p_eff is None else f"{p_eff:.4f}",
                    "" if k_eff is None else f"{k_eff:.4f}",
                    "" if pnl is None else f"{pnl:.4f}",
                    res.detail,
                ])
        finally:
            await poly.close()
            await kalshi.close()

    # Full rewrite: kept-settled rows + freshly fetched rows (no stale-PENDING duplicates).
    # Atomic: write a sibling .tmp then os.replace, so a run killed mid-write (reboot, OOM,
    # systemctl stop) leaves the PRIOR complete CSV intact — never a truncated/partial file
    # that would corrupt the verdict denominator. _OUT + ".tmp" is in the SAME directory
    # (logs/), hence the same filesystem, which os.replace requires to be atomic. (Same
    # pattern as positions.py:_save.) assert_safe_output still guards _OUT — the real target
    # the rename lands on — not the transient .tmp, so the loss-cap-file bar is unmoved.
    assert_safe_output(_OUT)
    keep_as_lists = [[r.get(col, "") for col in _HEADER] for r in keep_rows]
    tmp = _OUT + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_HEADER)
        w.writerows(keep_as_lists)
        w.writerows(new_rows)
    os.replace(tmp, _OUT)
    print(f"wrote {len(keep_as_lists) + len(new_rows)} rows → {_OUT} "
          f"({len(keep_as_lists)} kept-settled, {len(new_rows)} re-fetched)")

    records = _load_existing_records()
    settled_now = sum(1 for r in records if r.get("outcome") != Outcome.PENDING.value)
    _, n_events, dedup_pnl, n_settled_events = _economic_events(records)
    print(f"\ntotal captured: {len(records)} rows  ({n_events} distinct economic events)  "
          f"settled: {settled_now}  pending: {len(records) - settled_now}")
    print(f"realized P&L (deduped to economic events): ${dedup_pnl:.4f} "
          f"across {n_settled_events} settled events")
    print("  (rows are per-ticker — one hedge logs on two settlement-equivalent tickers; the "
          "per-series audit below is also per-ticker.)")
    _print_audit(audit(records))


if __name__ == "__main__":
    asyncio.run(main())
