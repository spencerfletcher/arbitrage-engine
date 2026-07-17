"""
scripts/poly_book_freshness_probe.py
────────────────────────────────────
READ-ONLY diagnostic. Makes NO trades, touches NO execution-path code. Settles the
question the frozen-Poly-ask "phantom" verdict hinges on (docs/TODO.md Watch,
memory frozen-poly-ask-phantom): **is the Poly book `transactTime` a content-freshness
signal at all?**

The fire-path origin-freeze gate (G1) trusts `transactTime` to mean "last book
MUTATION time" — i.e. it ASSUMES a frozen book keeps re-serving the SAME stale
`transactTime`, so its age grows and the gate can flag it (bot/poly_us/feed.py:69-77,
explicitly "assumed ... not verified"). The Poly docs define the field only as
"Timestamp of data." If it is instead a SERVE/response time (refreshed on every read),
then a frozen book returns a fresh `transactTime` on each poll, G1's age is ~0, and the
gate is fictional — which would explain would_fire wf 62/63 (transactTime 0.14s on a Poly
ask that had been unchanged ≥35s while Kalshi moved).

METHOD (corrected 2026-07-14 — see the BUG note below). Two tests, both on cache-busted
(fresh=True, cf=MISS, origin) reads:

  TEST A — PAIRED back-to-back reads ~150ms apart (the decisive one). A book cannot
  genuinely mutate between two reads 150ms apart on a quiet market, so for pairs whose
  FULL book is byte-identical:
    • transactTime ADVANCED (by ~= the wall gap) → SERVE-TIME / clock → G1 fictional (cause (a))
    • transactTime HELD                          → MUTATION-TIME     → G1's assumption valid
  TEST B — a fixed-cadence time series (raw data + the live-vs-dead-book signal).

⚠️ BUG FIXED 2026-07-14: the original discriminator compared only the TOP ASK across
samples. That is confounded — a book mutates at depth (size/deeper levels) while the top
ask holds, which advances transactTime legitimately. Measured live: `ask 0.38 -> 0.38`
with a real deep mutation and tt_advanced=True. The old ask-only test scored those as
"ask unchanged + tt advanced" and would have returned a FALSE "SERVE-TIME / G1 is
fictional" verdict. Both tests now compare a FULL-BOOK signature (every level's price+qty).

FINDING (2026-07-14, quiet pre-game WorldCup/MLB books, n=56 identical-book pairs):
  transactTime HELD 56/56 on identical books; advanced 48/48 when the book genuinely
  changed; ages on quiet books were 2.4–9.4s (a serve-time clock would read ~0).
  ⇒ transactTime is LAST-MUTATION time. G1's assumption is VALID and rest_transact_age_s
  IS a real content-freshness signal. Caveat: quiet PRE-GAME regime only — re-run during a
  live/hot book (the regime the phantoms live in) to confirm it generalises.

NOT covered here (phase 2, needs the live WS feed): WS-vs-REST ask agreement at the same
instant. Get that by diffing the running bot's would_fire_samples.csv / the _probe_subsecond
rows, or extend this probe to spin up PolyUSFeed.

Run:  .venv/bin/python -m scripts.poly_book_freshness_probe [duration_s] [interval_s]
Writes: logs/poly_freshness_probe_<epoch>.csv (timestamped so a re-run never clobbers prior data)
"""
from __future__ import annotations

import asyncio
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from bot.core import config
from bot.poly_us.client import PolyUSClient, transact_age_s

_SERIES_NAMES = {"69": "WorldCup", "15": "MLB", "4": "NBA", "6": "NHL", "49": "WNBA"}
_WINDOW_H = (-5.0, 1.0)   # hours-to-start counted as "live / about to start"
_MAX_MARKETS = 12         # cap concurrent polled markets → bounded request rate
_PAIRS = 12               # TEST A: paired back-to-back reads per market. Sized for the HOT regime:
                          # on a live book most pairs mutate (only the byte-identical ones are a
                          # valid test set), so a quiet-book count would leave too few to rate-check.
_PAIR_GAP_S = 0.15        # TEST A: intra-pair gap (too short for a genuine mutation)


def _book_sig(ask, levels) -> str:
    """ASK-SPACE signature (TEST B only): any ask-side level's price/qty changing changes this.

    ⚠️ Ask-space is NOT the whole book — `get_fill_quote` returns only the tradeable side, so a
    BID-side mutation advances transactTime without changing this. Measured 2026-07-14: 7/46
    'ask-identical' pairs showed tt advancing at ratio 1.00 vs the wall gap, which looks exactly
    like serve-time but was bid-side churn. TEST A uses _full_book_sig instead — use that for any
    semantics verdict; TEST B's number is an UPPER bound on 'advanced'."""
    lv = ",".join(f"{p:.6f}:{q:.6f}" for p, q in (levels or []))
    return f"{'' if ask is None else round(ask, 6)}|{lv}"


def _full_book_sig(md: dict) -> tuple:
    """COMPLETE-book signature from raw marketData — every bid AND every offer, plus state.

    Uses the raw string px/qty (no float rounding), so ANY mutation on EITHER side changes it.
    This is what makes TEST A decisive: if transactTime advances while this is identical, nothing
    in the book changed, so tt cannot be a mutation stamp → serve-time."""
    def side(k: str) -> tuple:
        return tuple((x["px"]["value"], x["qty"]) for x in (md.get(k) or []))
    return (side("bids"), side("offers"), md.get("state"))


def _hours_to_start(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (t - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except ValueError:
        return None


def _is_winner(sports_market_type: str) -> bool:
    return "winner" in (sports_market_type or "").lower()


async def _discover_live_slugs(sdk) -> list[tuple[str, str]]:
    """(label, market_slug) for in-window winner markets across configured series."""
    out: list[tuple[str, str]] = []
    for sid in config.POLYMARKET_US_SERIES:
        label = _SERIES_NAMES.get(sid, sid)
        try:
            resp = await sdk.events.list(params={
                "seriesId": sid, "active": True, "closed": False, "limit": 100, "offset": 0})
        except Exception as exc:
            print(f"  {label}: discover error {exc!r}")
            continue
        for ev in (resp.get("events", []) if isinstance(resp, dict) else []):
            hrs = _hours_to_start(ev.get("startTime") or ev.get("startDate"))
            if hrs is None or not (_WINDOW_H[0] <= hrs <= _WINDOW_H[1]):
                continue
            for m in ev.get("markets", []):
                if _is_winner(m.get("sportsMarketType", "")) and m.get("slug"):
                    out.append((f"{label}:{m['slug']}", m["slug"]))
    return out


async def _paired_test(client, slugs: list[tuple[str, str]]) -> None:
    """TEST A (decisive): back-to-back reads ~150ms apart — too short for a genuine mutation.

    On pairs whose FULL book is byte-identical: tt advancing by ~the wall gap ⇒ serve-time (clock);
    tt holding ⇒ last-mutation time. tt_delta is derived from transact_age_s against a common ref,
    so no second ISO parser is needed."""
    recs: list[dict] = []
    for _ in range(_PAIRS):
        t1 = time.time()
        r1 = await asyncio.gather(*(client._fetch_book(s, fresh=True) for _, s in slugs),
                                  return_exceptions=True)
        await asyncio.sleep(_PAIR_GAP_S)
        t2 = time.time()
        r2 = await asyncio.gather(*(client._fetch_book(s, fresh=True) for _, s in slugs),
                                  return_exceptions=True)
        ref = time.time()
        for (label, _slug), a, b in zip(slugs, r1, r2):
            if isinstance(a, Exception) or isinstance(b, Exception):
                continue
            try:
                ma, mb = a["marketData"], b["marketData"]
            except (TypeError, KeyError):
                continue
            age1 = transact_age_s(ma.get("transactTime"), ref)
            age2 = transact_age_s(mb.get("transactTime"), ref)
            if age1 is None or age2 is None:
                continue
            recs.append({"label": label, "same_book": _full_book_sig(ma) == _full_book_sig(mb),
                         "tt_delta": age1 - age2, "wall": t2 - t1})
        await asyncio.sleep(1.0)

    same = [r for r in recs if r["same_book"]]
    print("\n" + "=" * 72)
    print(f"TEST A (DECISIVE) — PAIRED reads ~{_PAIR_GAP_S*1000:.0f}ms apart, COMPLETE-book compare "
          f"(all bids + all offers + state): {len(recs)} pairs, {len(same)} fully identical "
          f"({len(recs)-len(same)} mutated)")
    if not same:
        print("  No fully-identical pairs — book too active to isolate. Rely on TEST B (weaker),")
        print("  or re-run with a shorter gap / calmer market.")
        return
    adv = [r for r in same if r["tt_delta"] > 0.001]
    held = [r for r in same if abs(r["tt_delta"]) <= 0.001]
    print(f"  transactTime ADVANCED on a COMPLETELY unchanged book: {len(adv)}/{len(same)}")
    print(f"  transactTime HELD     on a COMPLETELY unchanged book: {len(held)}/{len(same)}")
    # Per-market breakdown FIRST — a single run can span REGIMES: the -5..+1h window admits a live/hot
    # game AND a not-yet-started book from another series (e.g. an NBA tip-off 50min out). Aggregating
    # blends them, and quiet books pad "identical+held", diluting the hot-regime signal this run exists
    # to measure. mutated% = how hot each book actually was; read the verdict on the HOT rows.
    bym: dict[str, dict] = {}
    for r in recs:
        d = bym.setdefault(r["label"], {"n": 0, "same": 0, "adv": 0})
        d["n"] += 1
        if r["same_book"]:
            d["same"] += 1
            if r["tt_delta"] > 0.001:
                d["adv"] += 1
    print("  per-market (mutated% = how hot the book was; judge the verdict on the HOT rows):")
    for lbl, d in sorted(bym.items()):
        mut = 100.0 * (d["n"] - d["same"]) / d["n"] if d["n"] else 0.0
        ar = f"{100.0*d['adv']/d['same']:.0f}%" if d["same"] else " n/a"
        print(f"    {lbl[:46]:46} pairs={d['n']:3} identical={d['same']:3} "
              f"mutated={mut:3.0f}%  advanced={ar:>4}")

    rate = len(adv) / len(same)
    ratio = None
    if adv:
        med = lambda v: sorted(v)[len(v) // 2]
        d, w = med([r["tt_delta"] for r in adv]), med([r["wall"] for r in adv])
        ratio = d / w if w else None
        print(f"  among ADVANCED: median tt_delta {d:.3f}s vs wall {w:.3f}s → ratio {ratio:.2f}")
        for r in adv[:4]:
            print(f"     {r['label'][:38]:38} tt_delta {r['tt_delta']:+.3f}s (wall {r['wall']:.3f}s)")
    # Discriminate on RATE + RATIO, not on presence. A sporadic advance on a byte-identical book is
    # EXPECTED under last-mutation semantics: a mutate-and-revert inside the gap (an order placed then
    # cancelled) leaves the book identical while genuinely mutating twice, so tt legitimately advances
    # — and its delta is scattered (ratio ≠ 1.0, and can exceed 1.0 since read1's last mutation may
    # predate read1). Serve-time is the opposite: EVERY response is restamped, so ~100% of identical
    # pairs advance by exactly the wall gap (ratio ≈ 1.00). Rate is the signal; presence is not.
    print(f"  advance rate on unchanged books: {rate:.0%}")
    if rate >= 0.5 and ratio is not None and 0.90 <= ratio <= 1.10:
        print("  VERDICT: ⚠️ SERVE-TIME — nearly every unchanged-book read is restamped at ~1.0x the")
        print("    wall gap. G1's age signal is FICTIONAL; a content gate is required (cause (a)).")
    elif rate <= 0.10:
        print("  VERDICT: LAST-MUTATION time → G1's assumption VALID; rest_transact_age_s IS a real")
        print("    content signal (cause (b): the frozen-ask suspects may be REAL, and the 2s WS")
        print(f"    recency gate may be over-rejecting them). The {len(adv)} sporadic advance(s) are")
        print("    consistent with mutate-and-revert inside the gap, not restamping.")
    else:
        print(f"  VERDICT: AMBIGUOUS — {rate:.0%} advance rate, ratio {ratio}. Neither clean")
        print("    last-mutation (≤10%) nor serve-time (≥50% at ratio ~1.0). Re-run on a calmer book;")
        print("    do NOT build a gate on this result.")


async def main() -> None:
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    interval_s = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    client = PolyUSClient()
    try:
        slugs = await _discover_live_slugs(client._sdk)
        if not slugs:
            print(f"No in-window winner markets in {_WINDOW_H}h. Nothing live to probe.")
            return
        slugs = slugs[:_MAX_MARKETS]
        print(f"Probing {len(slugs)} markets every {interval_s}s for {duration_s}s "
              f"(fresh=True, cf=MISS):")
        for label, _ in slugs:
            print(f"  • {label}")

        await _paired_test(client, slugs)

        rows: list[dict] = []
        deadline = time.time() + duration_s
        while time.time() < deadline:
            now = time.time()
            results = await asyncio.gather(
                *(client.get_fill_quote(slug, fresh=True) for _, slug in slugs),
                return_exceptions=True,
            )
            for (label, slug), res in zip(slugs, results):
                if isinstance(res, Exception):
                    continue
                ask, state, levels, transact_time, _stats = res
                top_qty = levels[0][1] if levels else None
                rows.append({
                    "book_sig": _book_sig(ask, levels),
                    "wall_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "wall_epoch": f"{now:.3f}",
                    "market": label,
                    "slug": slug,
                    "ask": "" if ask is None else f"{ask:.4f}",
                    "transact_time": transact_time or "",
                    "transact_age_s": (lambda a: f"{a:.3f}" if a is not None else "")(
                        transact_age_s(transact_time, now)),
                    "state": state,
                    "top_qty": "" if top_qty is None else f"{top_qty:.0f}",
                    "n_levels": len(levels),
                })
            await asyncio.sleep(max(0.0, interval_s - (time.time() - now)))

        # ── persist raw series ────────────────────────────────────────────────
        if rows:
            out = f"logs/poly_freshness_probe_{int(time.time())}.csv"  # timestamped: never clobber
            with open(out, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            print(f"\nWrote {len(rows)} samples → {out}")

        _analyze(rows)
    finally:
        await client.close()


def _analyze(rows: list[dict]) -> None:
    """The discriminator: on consecutive same-ask samples, does transactTime advance?"""
    by_market: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_market[r["market"]].append(r)

    adv_unchanged = held_unchanged = 0   # ask unchanged: tt advanced vs held
    adv_changed = 0                      # ask changed: tt advanced (sanity)
    print("\n" + "=" * 72)
    print("PER-MARKET (ask moved over window? = live vs dead book)")
    for mkt, samples in sorted(by_market.items()):
        samples.sort(key=lambda r: float(r["wall_epoch"]))
        asks = [r["ask"] for r in samples if r["ask"] != ""]
        moved = len(set(asks)) > 1
        for a, b in zip(samples, samples[1:]):
            if not a["transact_time"] or not b["transact_time"] or a["ask"] == "" or b["ask"] == "":
                continue
            tt_advanced = b["transact_time"] > a["transact_time"]
            # FULL-book compare (NOT ask-only — see the BUG note in the module docstring).
            if a["book_sig"] == b["book_sig"]:
                if tt_advanced:
                    adv_unchanged += 1
                else:
                    held_unchanged += 1
            elif tt_advanced:
                adv_changed += 1
        print(f"  {mkt:34} n={len(samples):3} ask_moved={moved} "
              f"distinct_asks={len(set(asks))}")

    total_unchanged = adv_unchanged + held_unchanged
    print("\n" + "=" * 72)
    print("TEST B (CORROBORATION ONLY — ask-space, weaker than TEST A):")
    print("  Compares the ASK-SPACE book only (get_fill_quote returns just the tradeable side), so a")
    print("  BID-side mutation advances tt without changing it. 'ADVANCED' here is therefore an UPPER")
    print("  BOUND, inflated by bid churn — measured 2026-07-14: 7/46 such pairs were bid-side, not")
    print("  serve-time. Do NOT read a semantics verdict off this; TEST A (complete book) decides.")
    if total_unchanged == 0:
        print("  No unchanged ask-space pairs captured — rerun on a calmer/longer window.")
    else:
        pct = 100.0 * adv_unchanged / total_unchanged
        print(f"  transactTime ADVANCED while ask-space book unchanged: {adv_unchanged}/{total_unchanged} "
              f"({pct:.0f}%)  ← upper bound (bid churn included)")
        print(f"  transactTime HELD     while ask-space book unchanged: {held_unchanged}/{total_unchanged}")
        print(f"  (sanity) transactTime advanced when the ask-space book CHANGED: {adv_changed} "
              f"— expect ~100% of changes; if ~0, tt is dead and both tests are void.")


if __name__ == "__main__":
    asyncio.run(main())
