"""Kalshi ↔ Poly cross-platform arb: discovery, concurrent both-IOC execution,
leg reconciliation + unwind, price refresh, and the detection loop. Mixed into
BotRunner. This module owns the execution invariant — both legs hedged or any
unhedged excess unwound/stranded, never a silent one-sided position."""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import NamedTuple

from bot.core import config
from bot.core.logger import get_logger
from bot.core.alerts import send_kalshi_arb_alert, send_proper_edge_alert, send_schema_drift_alert
from bot.core.matcher import fetch_team_normalization
from bot.kalshi.client import (
    kalshi_avg_fill_cost, kalshi_avg_sell_proceeds, kalshi_filled_qty,
)
from bot.kalshi.matcher import match_kalshi_events, is_settlement_equivalent
from bot.kalshi.cross_arb import (
    find_kalshi_arbs, find_kalshi_tightest, KalshiArbOpportunity,
    _kalshi_taker_fee, _kalshi_breakeven_ask, kalshi_tick_floor,
    resize_opportunity,
    _effective_share_cost,
)
from bot.kalshi.macro_pairs import MacroPair
from bot.core.trade_logger import log_trade
from bot.poly_us.client import (
    _amount_to_float, poly_avg_fill_cost, quote_from_md, transact_age_s,
)
from bot.poly_us.sides import parse_token
from bot.runner.common import (
    fills_log, tightest_log, orderbook_log,
    _MISS_ALERT_THROTTLE_S, _BOOK_LOG_THROTTLE_S, _REJECT_LOG_THROTTLE_S, _fok_buffer,
    fmt_or_blank, schema_drift_alerts, in_window_slugs,
)

log = get_logger(__name__)


def _classify_fill_outcome(poly_fill: bool, kalshi_fill: bool) -> str:
    """Fill-success outcome for one detected edge at the fresh-REST re-read (observability only):
    both_fill = both legs fillable at intended price+size; poly_moved / kalshi_moved = that leg no
    longer fillable; both_moved = neither. Pure — unit-pinned; the fire path never branches on it."""
    if poly_fill and kalshi_fill:
        return "both_fill"
    if poly_fill:
        return "kalshi_moved"
    if kalshi_fill:
        return "poly_moved"
    return "both_moved"


def _interleg_kill(entry_depth_t0: float, t1, shares: float):
    """§5 classification (observability only — the fire path never branches on it). Would the Kalshi
    leg have killed in the poly-fill→kalshi-fire window? 1 = the entry-depth gate passed (t0>=shares)
    but a round-trip later the leg can't fill the order (t1<shares); 0 = still fillable. Returns ""
    (blank, NOT 0) when t1 is unknown (probe read error) — a feed hiccup must never count as a kill.
    Pure — unit-pinned."""
    if t1 is None:
        return ""
    return int(entry_depth_t0 >= shares and t1 < shares)


def _book_levels_present(book: dict, side: str) -> bool:
    """True iff the read carries a PARSEABLE levels array for `side`. An EMPTY array counts as
    present — a genuinely empty book means a real FOK would kill, which is a true observation.

    Exists because `_fillable_from_book` cannot tell "empty book" from "unreadable response": it
    returns 0.0 for a missing key, a null levels value, a changed response shape, and a 200-with-
    error-body alike. That 0.0 is the SAFE direction on the fire path (not fillable → reject → don't
    fire), but the WRONG one in the §5 probe, where it manufactures a kill out of a feed hiccup and
    biases §5 against poly_first. Gate the probe's parse on this so an unreadable response logs BLANK
    (unknown), never a kill.

    OBSERVABILITY-ONLY — do NOT wire into the fire path, whose fail-closed 0.0 is deliberate. Two
    callers: `_probe_interleg_window` (above) and `_sample_book_evolution`'s `kalshi_fillable`,
    which needs the same split for the same reason — `_rest_book` returns None only when the read
    RAISES, so a junk 200 arrives as a good dict and would otherwise log a fake measured 0.

    Reads the BUY array (the one `_fillable_from_book` parses). The SELL side needs the opposite
    array, and `_kalshi_exit_from_book` reports that split itself. Pure; unit-pinned."""
    if not isinstance(book, dict):
        return False
    ob = book.get("orderbook_fp")
    if not isinstance(ob, dict):
        return False
    return isinstance(ob.get("yes_dollars" if side == "no" else "no_dollars"), (list, tuple))


def _fillable_from_book(book: dict, side: str, limit_price: float) -> float:
    """Contracts of `side` buyable at <= limit_price from a Kalshi REST orderbook snapshot
    (the `orderbook_fp` shape get_orderbook returns). Buying a side lifts the OPPOSING book's
    bids — a yes bid at p is a NO offer at (1-p), takeable when (1-p) <= limit i.e. p >= 1-limit.
    Garbage levels are skipped. Pure; shared by the fire-path gate and the reject-log fallback.

    NEVER RAISES — and the guarantee is load-bearing, because callers invoke this OUTSIDE their try:
    `_rest_fillable`'s guard covers get_orderbook, not the parse, so a raise here reaches the fire
    path; `_kalshi_rest_depth_str` runs inside _log_reject, where a LOGGING path could take down a
    decision path. Two shapes have already breached it: `book.get("orderbook_fp", {})` returns None
    when the key is PRESENT but null and `.get` on None threw AttributeError (hence the isinstance
    ladder below), and a DICT-shaped level made `float(lvl[0])` throw KeyError, which the old
    `except (TypeError, ValueError, IndexError)` did not catch (fixed 2026-07-15).

    The level parse therefore catches Exception rather than a type list. That is the point: a
    guarantee spelled as an enumeration only holds for the shapes we thought of, and the venue
    supplies the shapes. Per-level, so one bad level is skipped and the rest of the book still
    counts — the failure mode a broad except is right for.

    Fail direction here is 0.0 → not fillable → reject, i.e. the same as a genuinely empty book.
    That is right ON THE FIRE PATH: unknown depth must never fire. It is the WRONG direction in the
    §5 probe, where 0.0 manufactures a kill out of a feed hiccup — which is why the probe gates its
    parse on _book_levels_present instead. Same helper, opposite correct fail-direction by caller."""
    if not isinstance(book, dict):
        return 0.0
    ob = book.get("orderbook_fp")
    if not isinstance(ob, dict):
        return 0.0
    levels = ob.get("yes_dollars" if side == "no" else "no_dollars")
    if not isinstance(levels, (list, tuple)):
        return 0.0
    threshold = round(1.0 - limit_price, 6)
    total = 0.0
    for lvl in levels:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except Exception:   # deliberately broad — see NEVER RAISES above
            continue
        if px >= threshold:
            total += qty
    return total


def _loop_running() -> bool:
    """True iff an asyncio loop is running — i.e. it is safe to create_task from sync code."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def fire_limits(opp) -> tuple[float, float]:
    """(poly_limit, kalshi_limit) for an opportunity — the EXACT prices the fire path would send.

    Extracted so the detection-probe and the fire path cannot drift: a probe that derives limits
    its own way measures a bot we don't run. Pure; no I/O.

    ⚠️ THE TWO LIMITS ARE NOT SYMMETRIC, and `buf` means opposite things on each side:
      Poly   — we bid the ask, FLAT. No cushion. (S4, decided 2026-07-16 — see below.)
      Kalshi — the breakeven ask MINUS buf, i.e. buf is a MINIMUM-PROFIT FLOOR. A bigger buf makes
               this limit TIGHTER, not looser. It is the only thing `buf` still does.

    **S4 — the Poly cushion was dropped 2026-07-16, on measurement.** It used to be
    `round(poly_ask_raw + buf, 4)`, an adverse-move cushion: fill even if the ask ticks up between
    detection and firing. Measured against `fill_success.csv` (cross-game rows excluded, and
    deduped to economic states — 1291 raw rows were 9 tokens, one market logged 1261 times), it
    rescued NOTHING:
      • of the `poly_moved` events (Kalshi fillable, Poly not), 86% are priced out
      • deduped, the CLOSEST miss is 2.3c — 4.6x the ~0.005 cushion; median 12.7c
      • rescued by a 0.005 / 0.01 / 0.02 cushion: 0 / 0 / 0 of 14 states
    The Poly ask does not drift a tick inside a ~50ms window: it stays put (67.9% of fires) or
    JUMPS ~12c (30.8%). There is no middle band for a cushion to catch. The alternative on the
    table — ceil-to-tick, to make the cushion survive Poly's floor — would have OVERSHOT (a full
    1c on a 0.01-tick market when buf is 0.005, ~40-50% of a 2-2.5% edge on the fills that used
    it) to buy a rescue measured at zero.
    ⚠️ n=14 distinct states, mostly MLB: this is "no evidence it ever helps", not proof it cannot.
    Poly fires FIRST, so a Poly miss is a clean miss — no exposure, no cost — which is why losing
    the cushion is cheap even if the measurement is thin.

    Not the real constraint either way: 99.5% of the DOMINANT failure (`kalshi_moved`, 68.4% of
    outcomes) is `kalshi_fillable == 0` — the Kalshi book is EMPTY at our limit, which no limit
    or buffer setting can fix. See docs/TODO.md's orderbook-detection item.
    """
    buf = _fok_buffer(opp.edge)
    kalshi_limit = kalshi_tick_floor(_kalshi_breakeven_ask(opp.poly_ask, buf), opp.kalshi_tick)
    poly_limit = round(opp.poly_ask_raw, 4)
    return poly_limit, kalshi_limit


def _sell_fillable_from_book(book: dict, side: str, min_price: float) -> float:
    """Contracts of `side` we could SELL at >= min_price — i.e. can the unwind actually get out?

    The MIRROR of _fillable_from_book, and deliberately a separate function rather than a flag,
    because both the array AND the comparison invert:
      BUY  `side` lifts the OPPOSING book's bids  → read `{opposite}_dollars`, take px >= 1-limit
      SELL `side` hits `side`'s OWN bids          → read `{side}_dollars`,     take px >= min_price
    Getting this backwards silently reports the wrong leg's depth. Observability only; never raises
    (the level parse catches Exception, not a type list — a dict-shaped level throws KeyError on
    `lvl[0]`, which an enumerated tuple missed; see _fillable_from_book).
    """
    try:
        ob = book.get("orderbook_fp", {}) if isinstance(book, dict) else {}
        levels = ob.get(f"{side}_dollars") or []
        if not isinstance(levels, (list, tuple)):
            return 0.0
    except Exception:
        return 0.0
    total = 0.0
    for lvl in levels:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except Exception:
            continue
        if px >= min_price:
            total += qty
    return total


def _kalshi_effective(opp) -> float:
    """The DETECTION-time fee-inclusive cost per contract of the Kalshi leg.

    ⚠️ EXISTS BECAUSE THE TWO `*_ask` FIELDS ARE NOT THE SAME KIND OF NUMBER, and reading them
    symmetrically silently drops the Kalshi fee:
        opp.poly_ask      — EFFECTIVE, the Poly fee is already IN it
        opp.kalshi_ask    — RAW, the fee is NOT
        opp.poly_ask_raw  — raw (the one whose name says so)
    So `opp.kalshi_ask` is ~1.7c/share short of what the leg actually costs — against a 2c minimum
    edge, ~85% of it. This is the same arithmetic `check_kalshi_arb` uses to compute the edge in
    the first place (`kalshi_effective = kalshi_ask + _kalshi_taker_fee(kalshi_ask)`), so booking
    anything less is not a "detection-price fallback", it is a fee-free fallback.
    """
    return opp.kalshi_ask + _kalshi_taker_fee(opp.kalshi_ask)


def _kalshi_exit_from_book(book: dict, side: str) -> tuple[float | None, float | None]:
    """(best_bid, depth_at_it) for SELLING `side` on Kalshi — the Kalshi counterpart of
    _poly_exit_from_book, and the other half of the unwind price.

    The price is derived FROM the book, not passed in, and that is the whole point. The obvious
    version — take the WS ticker's best bid and ask this book how much sits at or above it — reads
    two different TRANSPORTS against a `>=` threshold. They can disagree by a tick at any instant,
    and then the answer is silently either 0.0 ("nothing to sell into") or the next level's depth,
    with no way to tell from the row which happened. Taking max(px) from the SAME list the depth is
    summed from means the top level(s) satisfy `px >= best` exactly — the same structural argument
    the sampler's REST ask reduction already makes.

    Note this is the depth at the BEST bid, not at a sell limit — _sell_fillable_from_book answers
    the latter, which is what the exec-order probe wants (it prices the real unwind at bid−2¢).

    THREE-STATE return, and it is not the (None, 0.0)-for-everything its Poly counterpart uses:
        (px, qty)      a readable book with bids
        (None, 0.0)    a readable book with NO bids — a true observation: nothing to sell into
        (None, None)   the levels array is missing / null / not a list — we did not READ a book,
                       so we know nothing. _fillable_from_book collapses this case into 0.0
                       because on the fire path unknown MUST reject, but in a log a 0 here would
                       be indistinguishable from a genuinely empty book (the same split
                       _book_levels_present exists for on the buy side).
    NEVER RAISES: the level loop also catches KeyError, which a dict-shaped level triggers on
    `lvl[0]` — it rides _sample_book_evolution's bare except, where a raise costs the WHOLE row
    rather than these two cells."""
    if not isinstance(book, dict):
        return None, None
    ob = book.get("orderbook_fp")
    if not isinstance(ob, dict):
        return None, None
    levels = ob.get(f"{side}_dollars")
    if not isinstance(levels, (list, tuple)):
        return None, None
    pxq: list[tuple[float, float]] = []
    for lvl in levels:
        try:
            pxq.append((float(lvl[0]), float(lvl[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    if not pxq:
        return None, 0.0
    best = max(p for p, _ in pxq)          # selling → we want the HIGHEST bid
    return best, sum(q for p, q in pxq if p >= best - 1e-9)   # epsilon: mirrors the Poly reader


def _kalshi_ask_from_book(book: dict, side: str) -> tuple[float | None, float | None]:
    """(best_ask, qty_at_it) for BUYING `side` on Kalshi — what the ticker's quoted ask CLAIMS.

    Exists to measure the stale-ticker phantom (docs/TODO.md's orderbook item). `_fillable_from_book`
    answers "is there depth at OUR limit" — a yes/no that says the ticker was wrong but not BY HOW
    MUCH. This reports the price the real book would actually sell us, so `rest_ask − ticker_ask`
    quantifies the ticker's error directly instead of leaving it inferred from an absence.
    Observability only (the detect probe); no gate reads it.

    Reads the SAME array `_fillable_from_book` sums, for the same reason: buying a side lifts the
    OPPOSING book's bids, so a yes bid at p IS a no offer at (1−p). Hence best ask = 1 − max(bid) —
    the highest bid is the cheapest offer. Taking max() from the array the depth comes from (rather
    than crossing to the ticker) is the argument `_kalshi_exit_from_book` makes: two transports
    compared against a threshold can disagree by a tick, and then the row cannot say which happened.
    Caller must pass the book from ONE `_rest_book` snapshot shared with the depth read.

    THREE-STATE, mirroring `_kalshi_exit_from_book` — and deliberately NOT `_fillable_from_book`'s
    collapse-to-0.0, which is right on the fire path (unknown MUST reject) and wrong here (a log
    that cannot separate "empty book" from "unread book" is what put an ambiguity note in
    data_regimes.md in the first place):
        (px, qty)      a readable book with offers
        (None, 0.0)    a readable book with NO offers on this side — a true observation
        (None, None)   levels missing/null/not-a-list — we did not read a book; we know nothing
    NEVER RAISES: catches KeyError too — a dict-shaped level throws it on `lvl[0]`, the shape that
    already breached an enumerated except once (see _fillable_from_book)."""
    if not isinstance(book, dict):
        return None, None
    ob = book.get("orderbook_fp")
    if not isinstance(ob, dict):
        return None, None
    levels = ob.get("yes_dollars" if side == "no" else "no_dollars")
    if not isinstance(levels, (list, tuple)):
        return None, None
    pxq: list[tuple[float, float]] = []
    for lvl in levels:
        try:
            pxq.append((float(lvl[0]), float(lvl[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    if not pxq:
        return None, 0.0
    best_bid = max(p for p, _ in pxq)      # highest opposing bid = cheapest offer of `side`
    return round(1.0 - best_bid, 6), sum(q for p, q in pxq if p >= best_bid - 1e-9)


def _poly_exit_from_book(md: dict, is_short: bool) -> tuple[float | None, float]:
    """(best_exit_price, depth_at_it) for selling a Poly leg — EXACTLY how sell_back picks its
    price, so the modelled cost matches what the real unwind would pay:
      long  → SELL_LONG at the best (max) bid          [md.bids]
      short → SELL_SHORT at 1 − best (min) long ask    [md.offers]
    (None, 0.0) when there is nothing to sell into. Observability only; never raises."""
    try:
        raw = (md.get("offers") if is_short else md.get("bids")) or []
        pxq: list[tuple[float, float]] = []
        for lvl in raw:
            px = _amount_to_float(lvl.get("px")) if isinstance(lvl, dict) else None
            if px is None:
                continue
            try:
                qty = float(lvl.get("qty") or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
            pxq.append((round(1.0 - px, 6) if is_short else px, qty))
        if not pxq:
            return None, 0.0
        best = max(p for p, _ in pxq)          # already in sell-space for both sides
        return best, sum(q for p, q in pxq if p >= best - 1e-9)
    except Exception:
        return None, 0.0


_SIZE_CURVE_POINTS: tuple[int, ...] = (8, 25, 50, 100, 250, 500, 1000)


def _kalshi_ask_levels(book: dict, side: str) -> list[tuple[float, float]]:
    """Kalshi REST book → ask-space [(ask_price, qty), ...] for `side`, CHEAPEST FIRST.

    Same convention as _fillable_from_book (buying `side` lifts the OPPOSING book's bids: a bid at
    p is an offer at 1-p) — the two MUST agree or the whole curve inverts (pinned in tests).
    Garbage levels are skipped; never raises (this is observability on the fire path — the level
    parse catches Exception, not a type list, because a dict-shaped level throws KeyError on
    `lvl[0]`; see _fillable_from_book)."""
    try:
        ob = book.get("orderbook_fp", {}) if isinstance(book, dict) else {}
        levels = ob.get("yes_dollars" if side == "no" else "no_dollars") or []
        if not isinstance(levels, (list, tuple)):
            return []
    except Exception:
        return []
    out: list[tuple[float, float]] = []
    for lvl in levels:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except Exception:
            continue
        ask = round(1.0 - px, 6)
        if 0.0 < ask < 1.0 and qty > 0:
            out.append((ask, qty))
    out.sort(key=lambda x: x[0])
    return out


def _walk_effective_cost(levels, n, cost_fn) -> float | None:
    """Total EFFECTIVE cost (incl. taker fee) to buy `n` contracts walking ask-space `levels`.

    `cost_fn(price, qty) -> per-share effective cost at THAT level`, so the fee is charged PER
    LEVEL. That matters: Kalshi's fee ceil()s the BATCH total then divides by count, so applying it
    once at the VWAP is NOT equivalent (pinned in tests). Returns None when the ladder cannot
    supply `n` — None means "not reachable", NOT zero edge (log-review trap #2)."""
    need, total = float(n), 0.0
    for px, qty in sorted(levels or [], key=lambda x: x[0]):
        if need <= 1e-9:
            break
        take = min(need, float(qty))
        total += cost_fn(px, take) * take
        need -= take
    return None if need > 1e-9 else total


def _realized_edge_at(poly_levels, kalshi_levels, n, poly_fee_rate: float) -> float | None:
    """Post-fee per-share edge ACTUALLY realizable at size `n`, walking BOTH ladders.

        edge(n) = 1 - (poly_cost(n) + kalshi_cost(n)) / n

    — the same `1 - poly_effective - kalshi_effective` identity check_kalshi_arb uses, generalised
    from top-of-book to a size-N walk. Fees come from the CANONICAL fns only (never re-derived —
    CLAUDE.md load-bearing rule); at n=1 on a flat book this reproduces check_kalshi_arb exactly."""
    if n <= 0:
        return None
    pc = _walk_effective_cost(poly_levels, n,
                              lambda p, q: _effective_share_cost(p, poly_fee_rate))
    kc = _walk_effective_cost(kalshi_levels, n,
                              lambda p, q: p + _kalshi_taker_fee(p, max(1, int(round(q)))))
    if pc is None or kc is None:
        return None
    return 1.0 - (pc + kc) / float(n)


def _size_curve(poly_levels, kalshi_levels, poly_fee_rate: float,
                points: tuple[int, ...] = _SIZE_CURVE_POINTS) -> dict | None:
    """The size-vs-edge DECAY: {n: realized_edge|None} + the profit-maximising size.

    Why this exists: the logged `edge` is TOP-OF-BOOK and decays toward 0 as you walk to the
    breakeven limit, so `edge x fillable` is circular and meaningless (a bigger edge just loosens
    the limit and sweeps more levels). This measures where the edge ACTUALLY dies — the one number
    that decides whether the strategy scales past the chosen 8-share testing ramp (CLAUDE.md
    Posture: size is a CHOICE, not a depth limit — only 3% of both_fills are depth-capped).

    `best_n` = argmax(n * edge(n)) — the profit-maximising size, the actual deliverable.
    Pure + total: returns None rather than raising, so it can never affect what fires."""
    try:
        if not poly_levels or not kalshi_levels:
            return None
        pts = {n: _realized_edge_at(poly_levels, kalshi_levels, n, poly_fee_rate) for n in points}
        live = [(n, e) for n, e in pts.items() if e is not None]
        if not live:
            return None
        best_n, best_profit = None, None
        for n, e in live:
            prof = n * e
            if best_profit is None or prof > best_profit:
                best_n, best_profit = n, prof
        return {"points": pts, "best_n": best_n, "best_profit": best_profit}
    except Exception:
        return None


def _minutes_since_kickoff(kickoff: str | None) -> float | None:
    """Minutes since game kickoff (now − start_date), NEGATIVE before kickoff; None if the kickoff
    is absent/unparseable. Logging-only — lets the offline classifier segment edges by game phase
    (pre-game illiquid-but-real vs mid-game, where the goal→suspend→stale-tick phantom regime
    lives). Reuses transact_age_s (now − ISO timestamp, in seconds)."""
    age = transact_age_s(kickoff, time.time())
    return age / 60.0 if age is not None else None


def _origin_freeze_reject(book_age: float | None, peers_stale: int,
                          max_age: float, min_peers: int) -> bool:
    """The origin-freeze fire-gate decision, pure so the exact behavior is pinned (money-path #7).

    Reject iff BOTH hold:
      • the firing book is freeze-suspect — its server transactTime is older than `max_age`; AND
      • ≥ `min_peers` OTHER tracked markets are simultaneously stale (cross-market = an origin-wide
        freeze, the non-ambiguous discriminator a single illiquid book cannot produce).

    Fail-safe by construction:
      • book_age None (no/unparseable transactTime) → NOT suspect → never rejected (the gate cannot
        manufacture a fire OR a false reject from missing freshness data);
      • a LONE stale book (peers_stale < min_peers) → NOT rejected (preserve the real
        illiquid-but-live edge tail — the §5 stable-deep-book false-positive this gate must avoid)."""
    if book_age is None or book_age <= max_age:
        return False
    return peers_stale >= min_peers


def _best_ask_from_book(book: dict, side: str) -> float | None:
    """Best ask to BUY `side`, derived from a Kalshi REST orderbook snapshot (bids only).
    Buying a side lifts the OPPOSING book's bids: best yes-ask = 1 - max(no bids); best
    no-ask = 1 - max(yes bids) — the SAME convention as bot/kalshi/feed.py:get_best_ask, so a
    REST-derived ask is directly comparable to the WS ask. Returns None if the opposing book is
    empty (no ask derivable). Pure; scans for max (robust to the API's level ordering) and skips
    garbage levels like _fillable_from_book."""
    ob = book.get("orderbook_fp", {}) if isinstance(book, dict) else {}
    levels = ob.get("yes_dollars" if side == "no" else "no_dollars") or []
    best_bid = None
    for lvl in levels:
        try:
            px = float(lvl[0])
        except (TypeError, ValueError, IndexError):
            continue
        if best_bid is None or px > best_bid:
            best_bid = px
    return round(1.0 - best_bid, 6) if best_bid is not None else None


# ── Fat-spike diagnostic logging (logs/fat_spike_samples.csv) ─────────────────────────────────
# On detection of a >5% edge, capture WS-vs-REST price+depth on both venues over a short burst so a
# fat edge can later be classified as WS-ghost / real-but-sub-window / observed-late. Pure
# observability — records what was true; never changes what fires. See docs and the plan for design.
_FAT_SPIKE_EDGE_THRESHOLD = 0.05   # edge > this (5%) triggers a capture
_FAT_SPIKE_BURST_N        = 4      # REST samples per spike (Poly public is ~1/s — keep tiny)
_FAT_SPIKE_INTERVAL_S     = 0.10   # spacing → samples at ~0/0.1/0.2/0.3s ≈ 300ms window
_FAT_SPIKE_SLOW_MS        = 500.0  # REST latency above this = throttled/slow (queue delay, not book age)


class _FatSpikeWS(NamedTuple):
    """Detection-instant WS snapshot, FROZEN by value at the hook and passed into the async
    sampler so the WS baseline never moves under the WS-vs-REST comparison."""
    poly_ask: float | None
    poly_depth: float | None
    kalshi_ask: float | None
    kalshi_depth: float | None


class _ExecOrderProbeMixin:
    """DRY-only measurement for the poly_first-vs-kalshi_first decision. Never fires, never raises.

    Answers the two questions §5 alone cannot:

    1. **Unwind cost, both venues, modelled from the books at fire time.** The exec-order decision
       is NOT "what does a Kalshi unwind cost" — it is `P(kalshi kills) x poly_flatten` vs
       `P(poly kills) x kalshi_unwind`. On 792 economic events the Kalshi leg is the one that
       fails (64.0% kalshi_moved vs 5.9% poly_moved), so poly_first flattens Poly on ~64% of
       events while kalshi_first unwinds Kalshi on ~5.9% — an ~11x frequency gap at the SAME
       27.8% both_fill capture. For poly_first to win, a Kalshi unwind would have to cost ~11x a
       Poly flatten. This measures whether it does, instead of asserting it doesn't.
       Both costs are computed EXACTLY as the real unwind paths compute them (Kalshi: IOC 2c under
       bid, matching _unwind_kalshi_excess; Poly: best bid / 1-best-ask, matching sell_back), so
       the numbers are what we would actually pay, not a proxy.

    2. **The kalshi_first mirror of §5** — after the KALSHI order RTT (17ms, measured), has the
       POLY book moved below `shares`? poly_first's window exposes the Kalshi book for 61ms;
       kalshi_first's exposes the Poly book for 17ms. §5 measured 0/163 race-losses for the
       former; this measures the latter rather than assuming it's smaller.

    Also logs whether each unwind could actually FILL at its price (exit depth >= shares) — a cost
    you cannot pay is a STRAND, and Kalshi is the thinner book (p10 top-of-book qty ~5).
    """

    async def _probe_edge_at_detection(self, opp) -> None:
        """REST-verify an edge the INSTANT it is first seen — BEFORE the 0.3s gate can kill it.
        DRY-only observability: never fires, never raises, never touches a gate.

        THE CATCH-22 THIS BREAKS. Everything downstream (would_fire, interleg, exec_order) only
        logs for edges that SURVIVE persistence, so the data needed to judge the gate can only be
        collected by removing the gate. On 2026-07-15 the whole slate produced 11 edges above the
        2% floor and the gate killed 8 — every probe logged zero rows. Waiting for more slates
        cannot fix that; it is structural.

        THE QUESTION. Of the edges the gate kills, how many were REAL at t=0? We already know the
        gate does NOT filter phantoms (61.2% of its survivors are dead at the REST read — a stale
        quote is maximally persistent, so it clears a stability test trivially). This measures the
        other side: what the gate COSTS.
          - killed edges that were REST-real at t=0  → the gate is destroying real arbs (S11).
          - killed edges that were phantom at t=0    → the gate is earning its 300ms after all.

        HOW TO READ IT. Join to `rejected_edges.csv` on (event, kalshi_ticker, kalshi_side, ~ts):
        a `not_persistent` row there carries `lived Xs`. An edge is RECOVERABLE only if it was
        t0_real AND lived long enough to survive execution — firing at t=0 still costs ~22ms REST
        + 61ms Poly ≈ 0.083s before the first leg lands, so a 0.07s edge is unreachable no matter
        what we do. Do NOT count t0_real alone as recoverable.

        Fires once per edge EPISODE (first-seen only, gated by _confirm_seen upstream) — 12.5/hour
        measured, ~289x under Poly's 1/s budget. A per-TICK check would be ~100/s and Poly's
        over-limit behaviour is a late/stale 200, which would manufacture the phantoms we're
        hunting.
        """
        try:
            t0 = time.time()
            poly_limit, kalshi_limit = fire_limits(opp)   # the EXACT prices the fire path would use
            # ONE book snapshot, two readings. Not _rest_fillable + a second read: the depth and the
            # ask MUST describe the same book or `rest_ask − ticker_ask` measures the gap between two
            # reads instead of the ticker's error. (_rest_book's ~1s cache would likely collapse them
            # anyway — "likely" is not an invariant to rest a measurement on.) entry_depth reproduces
            # _rest_fillable EXACTLY, including its None→0.0 fail-closed direction.
            kbook, poly_result = await asyncio.gather(
                self._rest_book(opp.kalshi_ticker),
                self._poly_fill_quote(opp.poly_token),
            )
            (live_poly_ask, poly_state, poly_levels, _tt, _stats), _ms = poly_result
            entry_depth = (0.0 if kbook is None
                           else _fillable_from_book(kbook, opp.kalshi_side, kalshi_limit))
            # Blank ≠ 0: None = we never read a book; 0.0 = we read one and it had no offers.
            rest_ask, rest_ask_qty = ((None, None) if kbook is None
                                      else _kalshi_ask_from_book(kbook, opp.kalshi_side))
            # >0 ⇒ ticker quoted CHEAPER than the book will sell → the phantom-manufacturing
            # direction, and the magnitude the orderbook item needs. <0 ⇒ stale-HIGH (a real edge we
            # never detect — see S13). Blank when the book was unread.
            ticker_err = None if rest_ask is None else rest_ask - opp.kalshi_ask
            read_ms = (time.time() - t0) * 1000.0

            poly_fillable = (sum(q for px, q in poly_levels if px <= poly_limit)
                             if poly_levels else 0.0)
            # "Real at t=0" = what the fire path would require: Poly OPEN and takeable at our
            # limit, Kalshi with at least one contract at ours. Same conditions, no firing.
            poly_ok = (poly_state == "MARKET_STATE_OPEN" and live_poly_ask is not None
                       and live_poly_ask <= poly_limit and poly_fillable >= 1)
            kalshi_ok = entry_depth >= 1
            self._detect_probe_csv.writerow([
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                opp.event_title, opp.kalshi_ticker, opp.kalshi_side, opp.shares,
                f"{opp.edge:.6f}",
                f"{opp.poly_ask_raw:.4f}", f"{poly_limit:.4f}",
                "" if live_poly_ask is None else f"{live_poly_ask:.4f}",
                str(poly_state), f"{poly_fillable:.0f}",
                f"{opp.kalshi_ask:.4f}", f"{kalshi_limit:.4f}", f"{entry_depth:.0f}",
                fmt_or_blank(rest_ask, "{:.4f}"), fmt_or_blank(rest_ask_qty, "{:.0f}"),
                fmt_or_blank(ticker_err, "{:+.4f}"),
                int(poly_ok), int(kalshi_ok), int(poly_ok and kalshi_ok),
                f"{read_ms:.0f}",
            ])
        except Exception as e:   # observability must NEVER break detection
            log.warning(f"_probe_edge_at_detection failed (non-fatal): {e!r}")

    async def _probe_exec_order(self, opp, poly_limit, kalshi_limit, poly_fillable) -> None:
        try:
            # ── 1. Kalshi unwind: exactly what _unwind_kalshi_excess would do ────────────────
            k_bid = self.kalshi_feed.get_best_bid(opp.kalshi_ticker, opp.kalshi_side) or 0.01
            k_sell = max(0.01, kalshi_tick_floor(k_bid - 0.02, opp.kalshi_tick))
            # From the EFFECTIVE entry — opp.kalshi_ask is the RAW ask and the fee is not in it
            # (see _kalshi_effective). Booking from the raw ask understated every unwind cost by
            # ~1.7c/share, and this probe IS the S15 input, so it understated the case for
            # kalshi_first specifically. ⚠️ Rows before 2026-07-16 are short by that fee — see
            # docs/data_regimes.md.
            k_unwind_cost = _kalshi_effective(opp) - k_sell   # $/share we'd eat unwinding Kalshi
            try:
                kbook = await self.kalshi_client.get_orderbook(opp.kalshi_ticker, depth=20)
                k_exit_depth = _sell_fillable_from_book(kbook, opp.kalshi_side, k_sell)
            except Exception:
                k_exit_depth = None                            # blank ≠ 0 (unknown, not empty)

            # ── 2. The mirror: sleep the KALSHI order RTT, then re-read POLY fresh ───────────
            t_start = time.time()
            await asyncio.sleep(config.KALSHI_MIRROR_PROBE_DELAY_MS / 1000.0)
            slug, is_short = parse_token(opp.poly_token)
            p_fillable_t1 = None
            try:
                # Canonical parser (NOT a re-implementation) so the mirror measures the same
                # ask-space the fire path sizes on; fresh=True → bypass the 30s CF cache.
                _ask, _state, ask_levels, _tt, _stats = await self.client.get_fill_quote(
                    opp.poly_token, fresh=True)
                if ask_levels:
                    p_fillable_t1 = sum(q for px, q in ask_levels if px <= poly_limit)
            except Exception:
                p_fillable_t1 = None
            delay_ms = (time.time() - t_start) * 1000.0        # MEASURED (sleep + read RTT)

            # ── 3. Poly flatten cost: exactly how sell_back picks its price ──────────────────
            p_exit_px, p_exit_depth = None, None
            try:
                raw = await self.client._fetch_book(slug, fresh=True)
                md = raw.get("marketData", {}) if isinstance(raw, dict) else {}
                p_exit_px, p_exit_depth = _poly_exit_from_book(md, is_short)
            except Exception:
                pass
            # From the EFFECTIVE entry (opp.poly_ask), not the raw ask — the Poly fee is real money
            # we paid to open the leg, so a flatten costs it too. Same correction as the Kalshi
            # line above; the two together decide S15.
            p_flatten_cost = (opp.poly_ask - p_exit_px) if p_exit_px is not None else None

            would_kill = _interleg_kill(poly_fillable, p_fillable_t1, opp.shares)
            self._exec_order_csv.writerow([
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                opp.event_title, opp.kalshi_ticker, opp.kalshi_side, opp.shares,
                f"{opp.edge:.6f}",
                # Kalshi unwind
                f"{opp.kalshi_ask:.4f}", f"{k_bid:.4f}", f"{k_sell:.4f}",
                f"{k_unwind_cost:.4f}",
                "" if k_exit_depth is None else f"{k_exit_depth:.0f}",
                # Poly flatten
                f"{opp.poly_ask_raw:.4f}",
                "" if p_exit_px is None else f"{p_exit_px:.4f}",
                "" if p_flatten_cost is None else f"{p_flatten_cost:.4f}",
                "" if p_exit_depth is None else f"{p_exit_depth:.0f}",
                # kalshi_first mirror
                f"{poly_fillable:.0f}",
                "" if p_fillable_t1 is None else f"{p_fillable_t1:.0f}",
                f"{delay_ms:.0f}", would_kill,
            ])
        except Exception as e:   # observability must NEVER break the fire path
            log.warning(f"_probe_exec_order failed (non-fatal): {e!r}")


class KalshiArbMixin(_ExecOrderProbeMixin):
    async def _kalshi_discovery_loop(self) -> None:
        """Periodically refresh matched Polymarket ↔ Kalshi event pairs."""
        if not config.KALSHI_API_KEY:
            log.info("KALSHI_API_KEY not set — Kalshi cross-platform arb disabled.")
            return
        # Wait for the Polymarket scanner to populate active_pairs before matching.
        while not self.active_pairs:
            await asyncio.sleep(5)
        while True:
            try:
                kalshi_markets = await self.kalshi_scanner.fetch_markets(config.KALSHI_SERIES)
                norm_table = self._norm_table or await asyncio.to_thread(fetch_team_normalization)
                matched = match_kalshi_events(
                    self.active_pairs, kalshi_markets, norm_table,
                )
                # Settlement-equivalence gate (single source of truth): only keep
                # pairs whose (Kalshi series × Poly settlement structure) is verified
                # to settle on identical outcome sets. Fail-closed — everything else
                # (knockout, unverified sports, unknown structure) is dropped here so
                # detection/execution never even see a non-hedgeable pair.
                self._kalshi_pairs = [
                    cp for cp in matched
                    if is_settlement_equivalent(
                        cp.kalshi_market.ticker, cp.poly_pair.settlement_type
                    )
                ]
                blocked = len(matched) - len(self._kalshi_pairs)
                log.info(
                    f"Kalshi discovery: {len(self._kalshi_pairs)} settlement-equivalent "
                    f"pairs ({blocked} blocked) from {len(kalshi_markets)} open markets"
                )
                # Schema-drift watch: warn if a sport has live Kalshi markets but 0 matched
                # cross-pairs (silent venue rename). Uses RAW `matched` (pre-settlement-filter)
                # so legit settlement drops don't false-alarm. Never raises into discovery.
                try:
                    self._check_schema_drift(matched, kalshi_markets)
                except Exception as exc:
                    log.error(f"schema-drift check failed (non-fatal): {exc!r}")
                # Prime the Kalshi feed cache with REST prices for matched markets.
                # The WS only sends incremental updates (no snapshot on subscribe),
                # so without this the cache stays empty during quiet periods.
                primed = 0
                for cp in self._kalshi_pairs:
                    km = cp.kalshi_market
                    if km.yes_ask > 0:
                        self.kalshi_feed.prime(km.ticker, km.yes_bid, km.yes_ask)
                        primed += 1
                if primed:
                    log.info(f"Kalshi feed: primed {primed} market prices from REST")
                # In orderbook mode, subscribe the WS to these matched tickers
                # (no-op under the default ticker source). Refreshes on rediscovery.
                self.kalshi_feed.set_book_tickers(
                    [cp.kalshi_market.ticker for cp in self._kalshi_pairs]
                )

                # Macro path piggybacks on this loop — refresh open status + prime prices.
                if self._macro_pairs:
                    macro_raw = await self.kalshi_client.fetch_markets_raw(
                        config.KALSHI_MACRO_SERIES
                    )
                    live = {m.get("ticker", ""): m for m in macro_raw}
                    still_open: list[MacroPair] = []
                    macro_primed = 0
                    for mp in self._macro_pairs:
                        km = live.get(mp.kalshi_ticker)
                        if km is None or km.get("status") not in ("open", "active"):
                            log.warning(
                                f"Macro pair dropped (market no longer open): "
                                f"{mp.kalshi_ticker}"
                            )
                            continue
                        still_open.append(mp)
                        yes_ask = float(km.get("yes_ask_dollars") or 0)
                        yes_bid = float(km.get("yes_bid_dollars") or 0)
                        if yes_ask > 0:
                            self.kalshi_feed.prime(mp.kalshi_ticker, yes_bid, yes_ask)
                            macro_primed += 1
                    self._macro_pairs = still_open
                    if macro_primed:
                        log.info(f"Macro feed: primed {macro_primed} market prices from REST")
            except Exception as e:
                log.error(f"Kalshi discovery error: {e}")
            await asyncio.sleep(300)

    def _check_schema_drift(self, matched, kalshi_markets) -> None:
        """Warn (once per episode, after a 2-cycle debounce) if BOTH venues clearly have a live
        slate — Kalshi lists markets AND Poly fetched a slate of raw game events — yet 0 matched
        cross-pairs result. The silent venue-schema-rename signal (2026-06-23: Poly renamed MLB's
        winner sportsMarketType → 0 pairs → 0 matches, blind ~5.5h). Observability ONLY: reads
        counts, never alters matching/execution.

        Sport key = the Kalshi series ticker (e.g. "KXMLBGAME"): the Poly side carries it as
        MarketPair.kalshi_series; the Kalshi side uses `ticker.split("-", 1)[0]` — the EXACT
        expression the cross-sport matcher gate uses (matcher.py:139), so the alert's key spaces
        align BY CONSTRUCTION with the matcher's own. `matched` is the RAW match output (pre
        settlement-equivalence filter), so legit settlement drops never false-alarm.

        GATES ON POLY RAW EVENTS, NOT PAIRS (2026-06-24 false-positive fix): the old "Kalshi has
        markets" oracle cried wolf every night — Kalshi lists scheduled markets all day, so between
        game slates (Poly has 0 events, Kalshi still listing) it fired despite nothing being wrong.
        Pairs can't distinguish (between-slates and a real break both build 0 pairs). Raw Poly
        EVENTS can: 0 events = no games (silent); a slate of unpaired events = a real break (fire);
        a single stranded unpaired event (1, e.g. the all-day WC market with no Kalshi counterpart)
        = below the min_poly_events floor (silent). See schema_drift_alerts for the floor + trade-off.
        """
        kalshi_counts = Counter(km.ticker.split("-", 1)[0] for km in kalshi_markets)
        poly_pair_counts = Counter(p.kalshi_series for p in self.active_pairs if p.kalshi_series)
        # Raw Poly EVENT counts per series (games actually fetched), from the Poly scanner's last
        # scan — the games-actually-present signal. Empty if the scanner hasn't run / non-US mode
        # → fail-quiet (.get → 0 → no false alarm).
        _scanner = getattr(self, "_poly_us_scanner", None)
        poly_event_counts = dict(getattr(_scanner, "last_raw_event_counts", {}) or {})
        matched_counts = Counter(
            cp.poly_pair.kalshi_series for cp in matched if cp.poly_pair.kalshi_series
        )
        to_alert, recovered = schema_drift_alerts(
            config.KALSHI_SERIES, kalshi_counts, poly_event_counts, poly_pair_counts,
            matched_counts, self._schema_drift_cycles, self._schema_drift_alerted,
        )
        for series, kalshi_n, poly_events, poly_pairs in to_alert:
            log.warning(
                f"🚨 SCHEMA DRIFT: {series} has {kalshi_n} live Kalshi markets and {poly_events} "
                f"live Poly events but 0 matched cross-pairs (Poly pairs={poly_pairs}) — likely a "
                f"venue rename; matching is blind."
            )
            send_schema_drift_alert(series, kalshi_n, poly_events, poly_pairs)
        for series in recovered:
            log.info(f"✅ Schema drift cleared: {series} cross-pair matching recovered.")

    async def _log_kalshi_depth(self, ticker: str, label: str) -> None:
        """Snapshot the Kalshi orderbook to fills.log after a thin/partial fill.

        Diagnostic only — called AFTER the order resolves (not on the hot path),
        throttled per ticker, so we capture the real resting volume when an
        insufficient-volume / partial happens without slowing execution.
        """
        now = time.time()
        if now - self._last_book_log.get(ticker, 0.0) < _BOOK_LOG_THROTTLE_S:
            return
        self._last_book_log[ticker] = now
        try:
            book = await self.kalshi_client.get_orderbook(ticker, depth=10)
            ob = book.get("orderbook_fp", {}) if isinstance(book, dict) else {}
            yes = ob.get("yes_dollars") or []
            no = ob.get("no_dollars") or []
            ytot = sum(float(q) for _, q in yes)
            ntot = sum(float(q) for _, q in no)
            fills_log.info(
                f"{label} | KALSHI BOOK {ticker} yes_total={ytot:.0f} no_total={ntot:.0f} "
                f"yes_top={yes[:3]} no_top={no[:3]}"
            )
        except Exception as e:
            fills_log.info(f"{label} | KALSHI BOOK fetch failed {ticker}: {e!r}")

    def _log_orderbook_health(self, divergent: list, elapsed: float = 60.0) -> None:
        """Once-a-minute orderbook-maintenance summary → logs/orderbook.log.

        Off the hot path. Surfaces coverage, seq progress, cumulative gaps/resnapshots,
        the REST-divergence audit, a sample book row, AND receive-loop profiling
        (msg rate + handler duty cycle) — if duty% is high, the handler can't keep up
        and seq gaps are CLIENT-side (the server won't fix them; lighten the handler).
        """
        h = self.kalshi_feed.book_health()
        n_pairs = len(self._kalshi_pairs)
        elapsed = max(elapsed, 1e-6)
        msg_rate = h["msgs"] / elapsed
        avg_handler_ms = (h["handler_s"] / h["msgs"] * 1000) if h["msgs"] else 0.0
        duty_pct = h["handler_s"] / elapsed * 100  # % of wall-time inside the handler
        change_rate = h.get("changes", 0) / elapsed
        recv = (
            f" | recv: {msg_rate:.0f} msg/s avg_handler={avg_handler_ms:.2f}ms "
            f"duty={duty_pct:.0f}% changes={h.get('changes', 0)} ({change_rate*60:.0f}/min)"
        )

        def _f(x):
            return f"{x:.2f}" if isinstance(x, (int, float)) else "?"

        if divergent:
            worst = max(divergent, key=lambda d: abs(d[1] - d[2]))
            audit = f"{len(divergent)} divergent (max {abs(worst[1] - worst[2]):.2f} on {worst[0]})"
        else:
            audit = "0 divergent"

        sample = ""
        if self._kalshi_pairs:
            tk = self._kalshi_pairs[0].kalshi_market.ticker
            depth_no = self.kalshi_feed.get_depth(tk, "no")
            depth_str = f"{depth_no:.0f}" if isinstance(depth_no, (int, float)) else "?"
            sample = (
                f" | sample {tk} yes_bid={_f(self.kalshi_feed.get_best_bid(tk, 'yes'))} "
                f"yes_ask={_f(self.kalshi_feed.get_best_ask(tk, 'yes'))} "
                f"no_ask={_f(self.kalshi_feed.get_best_ask(tk, 'no'))} depth_no={depth_str}"
            )

        orderbook_log.info(
            f"ORDERBOOK HEALTH | books={h['books']}/{n_pairs} seq={h['seq']} "
            f"gaps={h['gaps']} resnaps={h['resnaps']} | audit: {audit}{recv}{sample}"
        )

    def _log_size_curve(self, wf_id: int, opp, poly_levels) -> None:
        """Log the VWAP size-vs-edge decay for this would-fire (logs/size_curve.csv).

        PURE OBSERVABILITY. Both ladders are ALREADY in hand — Poly's from the fire-time quote, the
        Kalshi book from the ~1s `_rest_fillable` snapshot cache — so this adds NO network reads,
        and the whole body is wrapped: it can never raise into the fire path or change what fires.

        Why it exists: the logged `edge` is TOP-OF-BOOK and decays to ~0 at the breakeven limit, so
        `edge x fillable` is circular (a bigger edge just loosens the limit and sweeps more levels).
        This walks both ladders and records the post-fee edge actually realizable at each size —
        the one number that says whether the strategy scales past the chosen 8-share testing ramp."""
        try:
            hit = getattr(self, "_rest_depth_cache", {}).get(opp.kalshi_ticker)
            if not hit:
                return
            kalshi_levels = _kalshi_ask_levels(hit[1], opp.kalshi_side)
            # Recover the DETECTION-TIME Poly fee rate from the opp itself: poly_ask is the
            # effective cost, poly_ask_raw the raw price, and effective = p + rate*p*(1-p). No
            # plumbing, and it cannot drift from the rate the canonical edge actually used.
            den = opp.poly_ask_raw * (1.0 - opp.poly_ask_raw)
            rate = (opp.poly_ask - opp.poly_ask_raw) / den if den > 1e-9 else 0.0
            curve = _size_curve(poly_levels, kalshi_levels, rate)
            if curve is None:
                return
            pts = curve["points"]
            self._size_curve_csv.writerow([
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), wf_id, opp.event_title,
                opp.kalshi_ticker, opp.kalshi_side, f"{opp.edge:.6f}", opp.shares,
                f"{sum(q for _p, q in (poly_levels or [])):.0f}",
                f"{sum(q for _p, q in kalshi_levels):.0f}",
                *[("" if pts.get(n) is None else f"{pts[n]:.6f}") for n in _SIZE_CURVE_POINTS],
                curve["best_n"] if curve["best_n"] is not None else "",
                "" if curve["best_profit"] is None else f"{curve['best_profit']:.4f}",
            ])
        except Exception as e:   # observability only — never let it reach the fire path
            log.debug(f"size-curve log skipped (observability): {e!r}")

    def _log_would_fire(self, opp, poly_limit: float, kalshi_limit: float,
                        poly_fillable: float, kalshi_fillable: float,
                        transact_age: float | None = None,
                        poly_read_ms: float | None = None,
                        poly_stats: dict | None = None,
                        poly_levels=None) -> None:
        """Record a fully-gated would-fire opportunity (logs/would_fire.csv) + spawn a
        book-evolution sampler. The data source for scripts/settlement_backtest.py —
        captured in BOTH dry and live so a DRY run accumulates the pre-capital dataset.
        Both legs' values are FRESH REST FILLABLE-AT-LIMIT at fire-time (poly_fillable from
        the fire-time book quote, kalshi_fillable from _rest_fillable) — the authoritative
        record that each leg was buyable at the price we'd pay.

        transact_age = Poly book staleness at fire (now − marketData.transactTime, seconds);
        blank (not 0) when the book carried no transactTime (pre-fix / missing field) — a parser
        must not read empty as 0.0.
        poly_read_ms = wall-clock of the fire-path Poly book read (the cache-bust MISS cost). The
        fire path serves a ≤1s-old quote from the local cache without any HTTP read; there is no
        MISS cost to measure then, so it logs "cache_hit" rather than a blank or a misleading 0 —
        the column stays a clean numeric distribution of real origin reads, and a cache hit is
        explicit instead of an unexplained gap.

        Liquidity/activity columns (LOGGING-ONLY, phantom-vs-real context): Poly OI / last-trade /
        shares / notional (+ age stamps) from the fire-path book `poly_stats` (fresh-at-fire);
        Kalshi OI / volume / last-trade (+ age) live from the ticker WS (kalshi_feed.get_liquidity,
        guarded — blank if the feed is absent or ticker hasn't been seen / orderbook mode). All
        blank (never 0) when absent.

        poly_tick / kalshi_tick = each leg's own minimum price increment, recorded here rather than
        per-sample because a tick is static per market. Logging them changes nothing — but the two
        FIELDS are NOT alike, and only one is inert: `kalshi_tick` is a FIRE-PATH input (kalshi_
        tick_floor divides by it to floor the Kalshi limit, in fire_limits and _execute_kalshi_arb,
        and again to price the unwind in _unwind_kalshi_excess), while `poly_tick` is read by
        nothing but this line. Don't infer from their sitting together here that neither matters.

        They are recorded because the tick is PER-MARKET and varies WITHIN a series [VERIFIED
        2026-07-16; values in the venue-reference skill, don't restate them here], so it silently
        changed behaviour without any code change — which is what this column caught on its FIRST
        row (wf 265): the Poly buffer was a real 1-tick cushion on the 0.005 half of the slate and
        floored away to nothing on the 0.01 half. That split is why the cushion was dropped (S4,
        2026-07-16 → COMPLETED.md); poly_limit is the raw ask now, so the tick no longer decides
        anything on the Poly leg. poly_tick is blank when unread — never assume a default."""
        self._wf_seq += 1
        wf_id = self._wf_seq
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ps = poly_stats or {}
        _kf = getattr(self, "kalshi_feed", None)
        kl = (_kf.get_liquidity(opp.kalshi_ticker) if _kf is not None else None) \
            or (None, None, None, None)

        _g = fmt_or_blank   # blank-not-zero formatter (shared; see common.fmt_or_blank)

        self._wf_csv.writerow([
            wf_id, ts, opp.event_title, opp.poly_token, opp.kalshi_ticker, opp.kalshi_side,
            f"{opp.poly_ask_raw:.4f}", f"{opp.kalshi_ask:.4f}", f"{opp.edge:.6f}",
            opp.shares, f"{poly_limit:.4f}", f"{kalshi_limit:.4f}",
            f"{poly_fillable:.0f}", f"{kalshi_fillable:.0f}",
            f"{transact_age:.3f}" if isinstance(transact_age, (int, float)) else "",
            f"{poly_read_ms:.0f}" if isinstance(poly_read_ms, (int, float)) else "cache_hit",
            _g(ps.get("open_interest"), "{:.0f}"), _g(ps.get("oi_age_s"), "{:.1f}"),
            _g(ps.get("last_trade_px"), "{:.4f}"), _g(ps.get("last_trade_qty"), "{:.2f}"),
            _g(ps.get("last_trade_age_s"), "{:.1f}"),
            _g(ps.get("shares_traded"), "{:.0f}"), _g(ps.get("notional_traded"), "{:.0f}"),
            _g(kl[0], "{:.0f}"), _g(kl[1], "{:.0f}"), _g(kl[2], "{:.4f}"), _g(kl[3], "{:.1f}"),
            _g(_minutes_since_kickoff(getattr(opp, "kickoff", None)), "{:.1f}"),
            _g(getattr(opp, "poly_tick", None), "{:.4f}"),
            _g(getattr(opp, "kalshi_tick", None), "{:.4f}"),
        ])
        # VWAP size-vs-edge decay, joined on wf_id. Observability only; self-wrapped (never raises).
        self._log_size_curve(wf_id, opp, poly_levels)
        asyncio.create_task(self._sample_book_evolution(wf_id, opp, kalshi_limit))

        # Notify the dedicated edge channel — every would-fire is a real, fillable
        # edge (passed every gate). Throttle per (event:ticker:side) so a persistent
        # edge can't spam, and post off the hot path (HTTP in a thread).
        cache = getattr(self, "_last_edge_alert", None)
        if cache is None:
            cache = self._last_edge_alert = {}
        key = f"{opp.poly_event_id}:{opp.kalshi_ticker}:{opp.kalshi_side}"
        now = time.time()
        if now - cache.get(key, 0.0) > 60.0:
            cache[key] = now
            asyncio.create_task(asyncio.to_thread(
                send_proper_edge_alert, opp, poly_fillable, kalshi_fillable, config.DRY_RUN
            ))

    def _log_fill_success(self, opp, poly_limit: float, kalshi_limit: float, entry_depth: float,
                          live_poly_ask: float | None, poly_state: str, poly_levels,
                          poly_read_ms: float | None = None,
                          poly_transact: str | None = None) -> None:
        """Observability: one row per detected edge that reaches the fresh-REST re-read — whether
        BOTH legs are fillable at the intended prices+size when the re-read lands (logs/fill_success.csv).
        Called at the re-read point BEFORE the fire gates (which short-circuit), so it captures all
        four outcomes incl. both_moved. Pure side-effect; NO order, NO branch, NO state the gates read.
        Fully fail-safe: any error is swallowed so it can NEVER alter firing behavior. `fill_window_ms`
        = detection→re-read (an OPTIMISTIC proxy — omits the ~30ms order RTT; see the plan)."""
        if getattr(self, "_fs_csv", None) is None:
            return
        try:
            now = time.time()
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            poly_fillable = sum(q for p, q in (poly_levels or []) if p <= poly_limit)
            poly_fill = (poly_state == "MARKET_STATE_OPEN" and live_poly_ask is not None
                         and live_poly_ask <= poly_limit and poly_fillable >= opp.shares)
            kalshi_fill = entry_depth >= opp.shares
            outcome = _classify_fill_outcome(poly_fill, kalshi_fill)
            poly_slip = (live_poly_ask - opp.poly_ask_raw) if live_poly_ask is not None else None
            poly_short = max(0.0, opp.shares - poly_fillable)
            kalshi_short = max(0.0, opp.shares - entry_depth)
            det = getattr(opp, "detected_ts", None)
            fill_window_ms = (now - det) * 1000.0 if isinstance(det, (int, float)) else None
            transact_age = transact_age_s(poly_transact, now)
            self._fs_seq += 1

            _g = fmt_or_blank   # blank-not-zero formatter (shared; see common.fmt_or_blank)

            self._fs_csv.writerow([
                self._fs_seq, ts, opp.event_title, opp.poly_token, opp.kalshi_ticker, opp.kalshi_side,
                outcome, opp.shares, f"{opp.edge:.6f}",
                f"{opp.poly_ask_raw:.4f}", f"{poly_limit:.4f}",
                _g(live_poly_ask, "{:.4f}"), _g(poly_slip, "{:.4f}"),
                f"{poly_fillable:.0f}", f"{poly_short:.0f}", poly_state,
                f"{opp.kalshi_ask:.4f}", f"{kalshi_limit:.4f}",
                f"{entry_depth:.0f}", f"{kalshi_short:.0f}",
                _g(fill_window_ms, "{:.1f}"),
                f"{poly_read_ms:.0f}" if isinstance(poly_read_ms, (int, float)) else "cache_hit",
                f"{transact_age:.3f}" if isinstance(transact_age, (int, float)) else "",
                _g(_minutes_since_kickoff(getattr(opp, "kickoff", None)), "{:.1f}"),
            ])
        except Exception as e:  # observability must NEVER break the fire path
            log.warning(f"_log_fill_success failed (non-fatal, fire path unaffected): {e!r}")

    async def _sample_book_evolution(self, wf_id: int, opp, kalshi_limit: float) -> None:
        """Sample both books at +0/0.25/0.5/1/2/3s after a would-fire. The sub-second points
        capture the fill-race decay that 1s granularity missed (you reach Kalshi ~100–300ms
        after the Poly fill); they also flag edges already collapsing (staleness). Each row
        logs an AUTHORITATIVE REST Poly depth next to the freeze-prone WS depth so phantom
        depth can be caught by ground truth, not just inference. The logged offset is the
        ACTUAL elapsed time (REST latency makes nominal offsets drift). Best-effort; never
        raises.

        Both sides are sampled in BOTH directions: the ask/fillable columns price the ENTRY,
        the bid/bid-depth columns price the UNWIND. The bid depths are what make the flatten
        cost honest at size — ask−bid is only the top-of-book price, so it is a LOWER BOUND on
        what flattening costs, exact while the size fits at the best bid and optimistic the
        moment it doesn't."""
        _poly_feed = self._poly_feed_adapter if self._poly_us else self.feed
        t0 = time.time()
        prev = 0.0
        for offset in (0.0, 0.25, 0.5, 1.0, 2.0, 3.0):
            if offset > prev:
                await asyncio.sleep(offset - prev)
            prev = offset
            try:
                actual = time.time() - t0
                pd = _poly_feed.get_price(opp.poly_token)
                poly_ask = getattr(pd, "best_ask", None) if pd else None
                poly_depth = getattr(pd, "ask_depth", 0.0) if pd else 0.0
                # Poly BID = the price an unwind sells into → the settlement backtest's
                # flatten cost. Without it that branch is uncomputable (don't silently 0).
                poly_bid = getattr(pd, "best_bid", None) if pd else None
                # Authoritative REST book (sampler-only; NOT the fire path) to ground-truth the
                # freeze-prone WS poly_ask/poly_depth above, read in BOTH directions off ONE
                # fetch. quote_from_md is the same parser get_fill_quote runs, so the ask columns
                # still mirror exactly what the fire path would see; _poly_exit_from_book is how
                # sell_back itself prices a sale, so the bid columns are the real flatten rather
                # than a model of it.
                #
                # One fetch, not two, is a REQUIREMENT here and not tidiness: Poly allows ~1 req/s
                # sustained and THROTTLES over-limit instead of rejecting — a late, stale 200. A
                # second fetch for the bid would have taken this sampler to ~4 reads/s, degrading
                # the freshness of its own ask columns (and the fire path shares that budget) to
                # collect a number whose entire value is being fresh. It also means ask and bid
                # describe ONE instant, so the spread between them is real.
                #
                # REST and not the WS bid depth that would have been free: this decides whether
                # the flatten cost survives at size, which makes it a SIZING input — and sizing
                # off WS depth is the mistake that logged 726257 against a real book of 3.
                #
                # Guarded separately from the outer except: _fetch_book RAISES on a read error
                # (get_fill_quote swallows its own), and an unguarded raise here would abort the
                # whole sample row rather than blank the Poly cells.
                rest_ask: float | None = None
                rest_depth: float | None = None
                rest_transact_age: float | None = None
                rest_bid: float | None = None
                rest_bid_depth: float | None = None
                if self._poly_us:
                    try:
                        _slug, _is_short = parse_token(opp.poly_token)
                        _raw = await self.client._fetch_book(_slug, fresh=True)
                        _md = _raw.get("marketData", {}) if isinstance(_raw, dict) else {}
                        rest_ask, _rest_state, rest_levels, rest_transact, _rest_stats = \
                            quote_from_md(_md, _is_short)
                        rest_transact_age = transact_age_s(rest_transact, time.time())
                        # Both directions gate on their array actually BEING there — the Poly
                        # mirror of _book_levels_present, and what makes "blank = never read,
                        # 0 = read and nothing there" true on this side too. quote_from_md and
                        # _poly_exit_from_book both collapse "no marketData" / "key null" /
                        # "read, none there" into the same empty answer, so an unguarded call
                        # logs 0 — a claim about the market — for a body we never parsed.
                        # (_poly_exit_from_book is left as-is: the exec-order probe shares it and
                        # wants the fail-soft.) A short token's tradeable side is the bid ladder,
                        # so the two keys swap with it.
                        _ask_key = "bids" if _is_short else "offers"
                        _exit_key = "offers" if _is_short else "bids"
                        if isinstance(_md.get(_ask_key), (list, tuple)):
                            # Best level chosen STRUCTURALLY: best = min(prices) drawn from the
                            # same list, so the minimal level(s) satisfy p <= best EXACTLY — no
                            # fragile float-equality against a re-rounded rest_ask (which is also
                            # None when ask<=0 even though depth exists). Keeps best-LEVEL
                            # semantics (qty at the min ask), matching the old get_book_depth.
                            _best = min(p for p, _ in rest_levels) if rest_levels else None
                            rest_depth = (sum(q for p, q in rest_levels if p <= _best)
                                          if _best is not None else 0.0)
                        if isinstance(_md.get(_exit_key), (list, tuple)):
                            rest_bid, rest_bid_depth = _poly_exit_from_book(_md, _is_short)
                    except Exception as e:
                        log.debug(f"sampler poly book read failed for {opp.poly_token}: {e!r}")
                # ⚠️ The two Poly depth columns disagree on what an EMPTY book logs, and an
                # analyst comparing them will be bitten by it. A book with no asks leaves
                # rest_poly_depth BLANK (the `if rest_levels` guard above — pre-existing, and
                # settlement_backtest's freeze detector reads that column as its primary signal,
                # so this is not the change to alter it in). A book with no bids logs
                # rest_poly_bid_depth 0, because _poly_exit_from_book reports (None, 0.0) — the
                # honest reading, and distinguishable from an unreadable book, which blanks BOTH.
                # So: blank bid_depth = we never read; 0 = we read, nothing was there.
                k_ask = self.kalshi_feed.get_best_ask(opp.kalshi_ticker, opp.kalshi_side)
                # Kalshi BID = the price a Kalshi unwind sells into — the mirror of poly_bid above,
                # and the number that decides poly_first vs kalshi_first. It was missing because the
                # sampler was built for poly_first, which flattens POLY: the instrument only ever
                # watched the leg the incumbent design happens to unwind, so the alternative could
                # not be costed. Sampling it at each offset gives the unwind cost AT THE UNWIND
                # MOMENT (~1 RTT later), not just at detection.
                # Read it, never assume: a missing bid logs blank, not 0 (a 0 would price the unwind
                # as a total loss and make kalshi_first look absurd).
                k_bid = self.kalshi_feed.get_best_bid(opp.kalshi_ticker, opp.kalshi_side)
                # ONE REST book, both directions. Ticker mode keeps no WS book, so the feed's
                # fillable_qty would be None here; the buy side reads fillable-at-limit and the
                # sell side reads what we could dump at the bid. Reading them off a single
                # snapshot costs no extra request (and the fire path's 1s cache usually makes
                # even this one free) and — more importantly — makes the two describe the SAME
                # book rather than two reads a round-trip apart.
                #
                # k_fill logs BLANK, not 0, when we did not READ a book. _rest_fillable maps that
                # to 0.0 because on the fire path 0 means SKIP, the safe direction; here there is
                # no decision to fail closed on, and a 0 would read as "book empty" — the blank≠zero
                # trap this project hits repeatedly. A parseable-but-EMPTY book still logs 0, which
                # is a real observation.
                #
                # Gating on _book_levels_present is what makes that true, and a `kbook is not None`
                # check would NOT: _rest_book returns None only when the read RAISES, so a 200
                # carrying an error body or a changed shape arrives as a perfectly good dict and
                # _fillable_from_book cannot tell it from an empty book — both give 0.0. That is
                # precisely the split this helper exists for (it already guards the §5 probe for
                # the same reason). It reads the BUY array, the one _fillable_from_book parses.
                kbook = await self._rest_book(opp.kalshi_ticker)
                k_fill = _fillable_from_book(kbook, opp.kalshi_side, kalshi_limit) \
                    if _book_levels_present(kbook, opp.kalshi_side) else None
                # What we could SELL, and how much of it — the mirror of rest_poly_bid/
                # rest_poly_bid_depth, and the missing half of the flatten cost. ask−bid prices
                # only the FIRST share: it assumes the whole size clears at the top of the book.
                # At 8 shares that never binds (the probe saw 382 and 3147 available); it is the
                # ~500-share question this is here to answer.
                #
                # Both come STRUCTURALLY from this one book, so rest_kalshi_bid_depth is the depth
                # at rest_kalshi_bid and not at some other price. Passing the WS k_bid in as the
                # threshold instead would read two transports against each other — a tick of
                # disagreement, and the depth silently reads 0 or the wrong level (see
                # _kalshi_exit_from_book). k_bid stays logged beside it as the WS-vs-REST
                # cross-check, exactly as poly_bid sits beside rest_poly_bid.
                #
                # It reports the unreadable-vs-empty split itself (None,None vs None,0.0), so it
                # needs no _book_levels_present gate — that helper reads the BUY array, which is
                # the wrong one for a sale.
                rest_k_bid, rest_k_bid_depth = _kalshi_exit_from_book(kbook, opp.kalshi_side)
                self._wf_samples_csv.writerow([
                    wf_id, f"{actual:.2f}",
                    f"{poly_ask:.4f}" if isinstance(poly_ask, (int, float)) else "",
                    f"{poly_depth:.0f}" if isinstance(poly_depth, (int, float)) else "",
                    f"{poly_bid:.4f}" if isinstance(poly_bid, (int, float)) else "",
                    f"{k_ask:.4f}" if isinstance(k_ask, (int, float)) else "",
                    f"{k_bid:.4f}" if isinstance(k_bid, (int, float)) else "",
                    f"{k_fill:.0f}" if isinstance(k_fill, (int, float)) else "",
                    f"{rest_k_bid:.4f}" if isinstance(rest_k_bid, (int, float)) else "",
                    f"{rest_k_bid_depth:.0f}" if isinstance(rest_k_bid_depth, (int, float)) else "",
                    f"{rest_depth:.0f}" if isinstance(rest_depth, (int, float)) else "",
                    f"{rest_ask:.4f}" if isinstance(rest_ask, (int, float)) else "",
                    f"{rest_bid:.4f}" if isinstance(rest_bid, (int, float)) else "",
                    f"{rest_bid_depth:.0f}" if isinstance(rest_bid_depth, (int, float)) else "",
                    f"{rest_transact_age:.3f}" if isinstance(rest_transact_age, (int, float)) else "",
                ])
            except Exception:
                pass

    async def _probe_interleg_window(self, opp, kalshi_limit: float,
                                     entry_depth_t0: float, poly_fillable: float) -> None:
        """§5 measurement (DRY-only, observability — never fires, never raises). After a would-fire,
        wait config.KALSHI_INTERLEG_PROBE_DELAY_MS (a stand-in for the real poly-fill→kalshi-fire
        round-trip), then re-read the Kalshi leg's fillable-at-limit depth FRESH (raw client, bypassing
        the 1s _rest_fillable cache) and log whether it dropped below `shares` — a simulated FOK kill in
        the inter-leg window. This is the load-bearing input to the poly_first-vs-kalshi_first decision
        and is otherwise unmeasurable in DRY (fills.log only fills on live execution).

        The §5 event is kalshi_would_kill==1 AND poly_fillable>=shares (poly would have filled first);
        poly_fillable is logged so the analysis applies that filter. entry_depth_t1 is left BLANK (not 0)
        when the depth is UNKNOWN — both on a raised read error and on a read that returns an
        unparseable body (see _book_levels_present) — so a feed hiccup is never miscounted as a kill
        (blank≠zero, project-wide). A parseable-but-empty book still logs t1=0 → a kill: that is a real
        observation, though it is market-unavailable rather than an adverse move. Read the two apart in
        analysis via entry_depth_t1: ==0 is an empty book, 0<t1<shares is a genuine book-moved-away.

        `delay_ms` is the MEASURED elapsed (sleep + the orderbook read RTT), NOT
        KALSHI_INTERLEG_PROBE_DELAY_MS — segment the log on it; eras are mixed in the file."""
        try:
            t_start = time.time()
            await asyncio.sleep(config.KALSHI_INTERLEG_PROBE_DELAY_MS / 1000.0)
            try:
                book = await self.kalshi_client.get_orderbook(opp.kalshi_ticker, depth=20)
                if _book_levels_present(book, opp.kalshi_side):
                    t1: float | None = _fillable_from_book(book, opp.kalshi_side, kalshi_limit)
                else:
                    # Read "succeeded" but carries no parseable levels → depth UNKNOWN, not zero.
                    log.debug(f"interleg probe: unparseable book for {opp.kalshi_ticker} "
                              f"— logging blank, not a kill")
                    t1 = None
            except Exception as e:
                log.debug(f"interleg probe read failed for {opp.kalshi_ticker}: {e!r}")
                t1 = None
            delay_ms = (time.time() - t_start) * 1000.0
            would_kill = _interleg_kill(entry_depth_t0, t1, opp.shares)
            self._interleg_probe_csv.writerow([
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                opp.event_title, opp.kalshi_ticker, opp.kalshi_side, opp.shares,
                f"{opp.edge:.6f}", f"{poly_fillable:.0f}", f"{entry_depth_t0:.0f}",
                f"{t1:.0f}" if isinstance(t1, (int, float)) else "",
                f"{delay_ms:.0f}", would_kill,
            ])
        except Exception as e:  # observability must NEVER break anything
            log.warning(f"_probe_interleg_window failed (non-fatal): {e!r}")

    async def _capture_fat_spike(self, arb_id: int, opp, ws: _FatSpikeWS, t_detect: float) -> None:
        """Burst-sample both venues' REST price+depth after a >5% edge, each row recording the FROZEN
        detection-instant WS snapshot `ws` alongside the fresh REST reading. Off the hot path
        (create_task'd from the edge-lifecycle loop); pure observability → logs/fat_spike_samples.csv,
        never fires, never raises. Each sample hits the RAW clients (self.client.get_fill_quote /
        self.kalshi_client.get_orderbook) directly, bypassing the 1s caches, so a ~300ms burst sees
        real snap-back rather than one cached book. Per-venue offset (detection→REST wall-clock),
        latency, and status (ok/slow/error) make a throttled Poly read identifiable, never silently
        trusted. NOTE: deduped once-per-lifecycle upstream → captures only the leading ~300ms of an
        edge, not its full decay arc (intentional, for Poly's ~1/s rate-limit safety)."""
        side = opp.kalshi_side

        _f = fmt_or_blank   # blank-not-zero formatter (shared; see common.fmt_or_blank)

        for i in range(_FAT_SPIKE_BURST_N):
            if i > 0:
                await asyncio.sleep(_FAT_SPIKE_INTERVAL_S)

            # ── Poly REST sample (fresh; depth + price from ONE snapshot, no WS contamination) ──
            p_offset_ms = (time.time() - t_detect) * 1000.0
            p_t0 = time.perf_counter()
            p_ask = p_state = p_depth = None
            p_transact_age = None       # REST book's server-side staleness (None until measured)
            p_stats: dict = {}          # liquidity/activity from the same book (logging-only)
            p_status = "ok"
            try:
                p_ask, p_state, p_levels, p_transact, p_stats = await self.client.get_fill_quote(
                    opp.poly_token, fresh=True)
                p_lat_ms = (time.perf_counter() - p_t0) * 1000.0
                p_transact_age = transact_age_s(p_transact, time.time())
                if p_levels:
                    _best = min(p for p, _ in p_levels)         # best-LEVEL depth, structural
                    p_depth = sum(q for p, q in p_levels if p <= _best)
                if p_lat_ms > _FAT_SPIKE_SLOW_MS:
                    p_status = "slow"                           # throttled/queue-delayed, not book age
            except Exception:
                p_lat_ms = (time.perf_counter() - p_t0) * 1000.0
                p_status = "error"

            # ── Kalshi REST sample (fresh get_orderbook; depth at REST-derived ask + ghost col) ──
            k_offset_ms = (time.time() - t_detect) * 1000.0
            k_t0 = time.perf_counter()
            k_rest_ask = k_depth = k_depth_ws = None
            # WS-ask staleness from the maintained feed (guarded: the unit-test stub has no feed).
            _kf = getattr(self, "kalshi_feed", None)
            k_ws_age = _kf.get_age(opp.kalshi_ticker) if _kf is not None else None
            k_status = "ok"
            try:
                book = await self.kalshi_client.get_orderbook(opp.kalshi_ticker, depth=20)
                k_lat_ms = (time.perf_counter() - k_t0) * 1000.0
                k_rest_ask = _best_ask_from_book(book, side)
                if k_rest_ask is not None:
                    # PRIMARY depth: at the REST snapshot's OWN ask (depth + price from one book).
                    k_depth = _fillable_from_book(book, side, k_rest_ask)
                if ws.kalshi_ask is not None:
                    # GHOST-revealing: REST depth at the frozen WS ask. Divergence vs k_depth IS the
                    # WS-ghost signature; the ONLY place a WS value touches a REST number, isolated.
                    k_depth_ws = _fillable_from_book(book, side, ws.kalshi_ask)
                if k_lat_ms > _FAT_SPIKE_SLOW_MS:
                    k_status = "slow"
            except Exception:
                k_lat_ms = (time.perf_counter() - k_t0) * 1000.0
                k_status = "error"

            try:
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                # Liquidity/activity (logging-only): Poly from this sample's book stats; Kalshi live
                # from the ticker WS (guarded — blank if no feed / unseen ticker / orderbook mode).
                _ps = p_stats or {}
                _getliq = getattr(_kf, "get_liquidity", None)
                _kliq = (_getliq(opp.kalshi_ticker) if _getliq is not None else None) \
                    or (None, None, None, None)
                self._fat_spike_csv.writerow([
                    ts, arb_id, opp.event_title, opp.poly_token, opp.kalshi_ticker, side,
                    f"{opp.edge*100:.2f}", i,
                    _f(ws.poly_ask, "{:.4f}"), _f(ws.poly_depth, "{:.0f}"),
                    f"{p_offset_ms:.0f}", f"{p_lat_ms:.0f}", p_status,
                    _f(p_ask, "{:.4f}"), _f(p_depth, "{:.0f}"), p_state if isinstance(p_state, str) else "",
                    # kalshi_ws_depth: None in ticker mode (no WS book maintained) reads as a mystery
                    # gap, so emit "ticker_mode" — the REST depth columns carry the real number. Test
                    # `is None`, NOT truthiness: a genuine 0-depth WS book must log "0", not the sentinel.
                    _f(ws.kalshi_ask, "{:.4f}"),
                    "ticker_mode" if ws.kalshi_depth is None else _f(ws.kalshi_depth, "{:.0f}"),
                    f"{k_offset_ms:.0f}", f"{k_lat_ms:.0f}", k_status,
                    _f(k_rest_ask, "{:.4f}"), _f(k_depth, "{:.0f}"), _f(k_depth_ws, "{:.0f}"),
                    _f(p_transact_age, "{:.3f}"), _f(k_ws_age, "{:.1f}"),
                    _f(_ps.get("open_interest"), "{:.0f}"), _f(_ps.get("oi_age_s"), "{:.1f}"),
                    _f(_ps.get("last_trade_px"), "{:.4f}"), _f(_ps.get("last_trade_qty"), "{:.2f}"),
                    _f(_ps.get("last_trade_age_s"), "{:.1f}"),
                    _f(_ps.get("shares_traded"), "{:.0f}"), _f(_ps.get("notional_traded"), "{:.0f}"),
                    _f(_kliq[0], "{:.0f}"), _f(_kliq[1], "{:.0f}"),
                    _f(_kliq[2], "{:.4f}"), _f(_kliq[3], "{:.1f}"),
                    _f(_minutes_since_kickoff(getattr(opp, "kickoff", None)), "{:.1f}"),
                ])
            except Exception:
                pass

    async def _unwind_kalshi_excess(self, opp, qty: int, label: str,
                                    entry_cost: float | None = None) -> None:
        """Flatten `qty` unhedged Kalshi contracts via IOC sell (2¢ under bid).
        Records the unsold remainder as stranded.

        `entry_cost` = the ACTUAL fee-inclusive cost per contract we paid, from the buy response.
        None → fall back to the DETECTION effective cost, loudly. See _kalshi_effective for why
        the fallback is not `opp.kalshi_ask`.
        """
        bid = self.kalshi_feed.get_best_bid(opp.kalshi_ticker, opp.kalshi_side) or 0.01
        sell_price = max(0.01, kalshi_tick_floor(bid - 0.02, opp.kalshi_tick))
        entry = entry_cost if entry_cost is not None else _kalshi_effective(opp)
        if entry_cost is None:
            log.warning(
                f"{label} | Kalshi unwind booking off the DETECTION price — actual fill cost "
                f"unreadable. The flatten loss reaching the cap will be optimistic by however far "
                f"the entry slipped."
            )
        # ⚠️ The try covers the SALE ONLY. It used to span the bookkeeping too, and that is not a
        # style point: if the sale succeeds and a later line raises, the `except` below strands
        # `qty` — the FULL position, including everything that just sold — and books a second cost
        # on top of the flatten already recorded. A double-counted loss and a strand record larger
        # than the real remainder, from an error that had nothing to do with the order.
        # This is not hypothetical: while writing this fix a missing import raised right here, and
        # the wide try turned a clean 10/10 sale into "unwind FAILED, 10 stranded". Disk-full is
        # the realistic trigger — architecture.md §6 says these CSVs are never rotated or deleted,
        # and `_record_exec_cost`'s writerow is unguarded.
        try:
            sell_resp = await self.kalshi_client.create_order(
                opp.kalshi_ticker, opp.kalshi_side, "sell",
                qty, f"{sell_price:.4f}", time_in_force="immediate_or_cancel",
            )
        except Exception as e:
            log.critical(f"Kalshi unwind FAILED: {e}. Recording stranded position.")
            fills_log.info(
                f"{label} | UNWIND kalshi FAILED for {qty} {opp.kalshi_ticker} "
                f"[{opp.kalshi_side}] @ {sell_price:.4f}: {e!r}"
            )
            self.tracker.add_stranded(
                opp.poly_event_id, opp.event_title,
                opp.kalshi_ticker, float(qty), entry * qty,
            )
            self._record_exec_cost("strand_kalshi", opp, qty, entry * qty, "unwind exception")
            return
        sold = qty if config.DRY_RUN else int(kalshi_filled_qty(sell_resp))
        fills_log.info(
            f"{label} | UNWIND kalshi sell {qty} @ {sell_price:.4f} → sold {sold} "
            f"| sell_resp={sell_resp!r}"
        )
        if sold > 0:
            # Exit at what the sale ACTUALLY netted, not at the limit we asked for. This IOC can
            # sweep several levels below `sell_price`, and it pays a fee on the way out — booking
            # the limit assumed we got the top of the book for free. `sell_resp` was discarded
            # entirely before 2026-07-16.
            exit_px = kalshi_avg_sell_proceeds(sell_resp)
            if exit_px is None:
                exit_px = sell_price
                log.warning(
                    f"{label} | Kalshi unwind booking the exit at the LIMIT — sell fill report "
                    f"unreadable ({sell_resp!r}). The flatten loss is optimistic by the sweep "
                    f"plus the sell fee."
                )
            self._record_exec_cost(
                "flatten_kalshi", opp, sold, (entry - exit_px) * sold,
                f"buy~{entry:.4f} sell~{exit_px:.4f}",
            )
        if sold < qty:
            rem = qty - sold
            log.critical(
                f"Kalshi unwind PARTIAL: sold {sold}/{qty}, {rem} stranded "
                f"for '{opp.event_title}' ({opp.kalshi_ticker})"
            )
            # `entry`, not opp.kalshi_ask: a strand books the FULL cost of a position we are still
            # holding, and the raw ask is ~1.7c/share short of it (see _kalshi_effective). This
            # number feeds BOTH the loss cap and total_exposure().
            self.tracker.add_stranded(
                opp.poly_event_id, opp.event_title,
                opp.kalshi_ticker, float(rem), entry * rem,
            )
            self._record_exec_cost("strand_kalshi", opp, rem, entry * rem, "partial unwind")

    async def _rest_book(self, ticker: str) -> dict | None:
        """A FRESH REST orderbook snapshot for `ticker`, or None if the read failed (never
        raises). None means UNKNOWN, not empty — callers must decide their own fail direction
        rather than inherit one.

        The snapshot is cached ~1s per ticker so a persistent phantom firing the depth gate
        every detection tick can't blow the REST rate limit. Both the buy-side gate and the
        sell-side sampler read through here, so a sampler running right after a gate reuses that
        one snapshot: no extra request, and both sides describe the SAME book rather than two
        reads milliseconds apart.

        The read-failure WARNING is deliberately emitted for every caller, observability
        included. It is not just noise-on-error: "0 `REST depth check failed` logs" is the
        evidence that the `kalshi_fillable=0` rows are genuinely-empty books rather than
        throttle-misreads — the reasoning behind a validated PROJECT_STATE claim and a
        venue-reference fact. Kalshi 429s rather than throttling, so a silenced read failure
        here would quietly re-open a question that log line is what closes."""
        cache = getattr(self, "_rest_depth_cache", None)
        if cache is None:
            cache = self._rest_depth_cache = {}
        now = time.time()
        hit = cache.get(ticker)
        if hit and now - hit[0] < 1.0:
            return hit[1]
        try:
            book = await self.kalshi_client.get_orderbook(ticker, depth=20)
        except Exception as e:
            # Keep the "REST depth check failed" prefix EXACTLY as-is — greps for it are the
            # evidence base for a validated PROJECT_STATE claim. The caller decides what a failure
            # costs (the gate skips, the sampler blanks), so the message no longer asserts one.
            log.warning(f"REST depth check failed for {ticker}: {e!r} — no book returned")
            return None
        cache[ticker] = (now, book)
        return book

    async def _rest_fillable(self, ticker: str, side: str, limit_price: float) -> float:
        """Contracts of `side` buyable at <= limit_price, from a FRESH REST orderbook
        snapshot (not the maintained WS book, which can freeze). Buying a side lifts the
        OPPOSING book's bids: a yes bid at p is a NO offer at 1-p, takeable for a NO buy
        when 1-p <= limit i.e. p >= 1-limit. Fail-closed: an unreadable book → 0.0 (→ skip)."""
        book = await self._rest_book(ticker)
        if book is None:
            return 0.0
        return _fillable_from_book(book, side, limit_price)

    async def _poly_fill_quote(
        self, token: str
    ) -> tuple[tuple[float | None, str, list[tuple[float, float]], str | None, dict], float | None]:
        """Fresh fire-time (quote, read_latency_ms) for `token`, where quote is
        (ask, market_state, ask_levels, transact_time, stats) and read_latency_ms is the wall-clock
        of the actual HTTP read (None on a local-cache hit → no HTTP happened). ask_levels is
        the side's depth in ASK space — the caller sums fillable-at-limit from it (levels are
        limit-independent, so caching by token stays correct).

        Delegates to PolyUSClient.get_fill_quote with fresh=True so the read bypasses the 30s
        Cloudflare cache and reaches origin (stale book data is worse than useless on the fire
        path). The 1s LOCAL cache is unchanged — it matches the price-age gate and bounds REST
        to ≤1 origin req/s/token, so a persistent phantom firing this gate every detection tick
        can't blow the rate limit; the nonce only flips the upstream CF key, not this cache."""
        cache = getattr(self, "_poly_quote_cache", None)
        if cache is None:
            cache = self._poly_quote_cache = {}
        now = time.time()
        hit = cache.get(token)
        if hit and now - hit[0] < 1.0:
            return hit[1], None            # local-cache hit: no HTTP → blank latency
        t0 = time.perf_counter()
        quote = await self.client.get_fill_quote(token, fresh=True)
        read_ms = (time.perf_counter() - t0) * 1000.0
        cache[token] = (now, quote)
        return quote, read_ms

    def _record_exec_cost(self, kind: str, opp, qty: int, amount: float, detail: str = "") -> None:
        """Record a realized execution cost (flatten round-trip or strand mark) to
        logs/execution_pnl.csv — the persistent source the cumulative loss cap reads
        (safety.cumulative_realized_loss). Only the positive part is a loss."""
        self._exec_pnl_csv.writerow([
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "execution_cost", kind, opp.event_title, opp.kalshi_ticker, qty,
            f"{amount:.4f}", detail,
        ])

    async def _unwind_poly_excess(self, opp, qty: int, label: str,
                                  entry_cost: float | None = None) -> None:
        """Flatten `qty` unhedged Poly contracts via sell_back; strand only what does NOT sell.

        `entry_cost` = the ACTUAL fee-inclusive cost per share we paid, from the buy response.
        None → fall back to `opp.poly_ask` (the DETECTION-time EFFECTIVE cost), loudly. It used to
        book `opp.poly_ask_raw`, which is the raw ask — fee-free, so the flatten loss reaching the
        cap was short by the Poly fee on top of any slippage.

        Poly rewrites our FOK → IOC [VERIFIED 2026-07-15], so the sell CAN PARTIAL-FILL. Symmetric
        with _unwind_kalshi_excess: book the proceeds of whatever sold, strand ONLY the remainder.
        (Was: sell_back returned a bare price, so a partial sale read as a total failure → the FULL
        qty was stranded, the real proceeds never reached P&L, and the retry re-offered shares that
        were already gone.)"""
        entry = entry_cost if entry_cost is not None else opp.poly_ask
        if entry_cost is None:
            log.warning(
                f"{label} | Poly flatten booking off the DETECTION price — actual fill cost "
                f"unreadable. The flatten loss reaching the cap will be optimistic by however far "
                f"the entry slipped."
            )
        price: float | None = None
        sold = 0.0
        try:
            if self._poly_us:
                price, sold = await self.client.sell_back(opp.poly_token, float(qty), label)
            else:
                # Legacy sync client — its own contract: a price means the WHOLE size sold.
                price = await asyncio.to_thread(
                    self.client.sell_back, opp.poly_token, float(qty), label)
                sold = float(qty) if price is not None else 0.0
        except Exception as e:
            price, sold = None, 0.0
            log.critical(f"Poly unwind error: {e}")
        fills_log.info(
            f"{label} | UNWIND poly sell_back {qty} {opp.poly_token} → sold {sold:.0f} @ {price!r}"
        )
        if sold > 0 and price is not None:
            # Realized flatten cost on what ACTUALLY sold: what we really paid, minus what the
            # sale really fetched. `entry` is the venue's own fill report when readable; the
            # fallback is the EFFECTIVE detection cost, never poly_ask_raw — that is the fee-free
            # ask, and booking it made the loss short by the Poly fee before slippage even counted.
            # RESIDUAL: `price` is sell_back's VWAP and excludes the Poly fee ON THE WAY OUT, so
            # the booked loss is still optimistic by that fee. sell_back would have to return the
            # commission for that; it returns (price, qty). Same on the Kalshi side until its
            # sell fill report is read (it now is). See docs/TODO.md.
            self._record_exec_cost(
                "flatten_poly", opp, int(sold), (entry - price) * sold,
                f"buy~{entry:.4f} sell~{price:.4f}",
            )
        rem = qty - int(sold)
        if rem > 0:
            log.critical(
                f"Poly US unwind {'PARTIAL' if sold else 'FAILED'} — sold {sold:.0f}/{qty}, "
                f"stranded {rem} {opp.poly_token} for '{opp.event_title}'"
            )
            self.tracker.add_stranded(
                opp.poly_event_id, opp.event_title,
                opp.poly_token, float(rem), opp.poly_ask * rem,
            )
            self._record_exec_cost(
                "strand_poly", opp, rem, opp.poly_ask * rem,
                "sell_back failed" if not sold else f"partial unwind {sold:.0f}/{qty}",
            )

    @staticmethod
    def _direction_key(opp) -> str:
        """Per-direction identity: event + the exact Kalshi leg. Cooldown keys off this
        (not the bare event) so each distinct hedged direction on a game fires on its own
        — one cooldown per position, not one per game."""
        return f"{opp.poly_event_id}:{opp.kalshi_ticker}:{opp.kalshi_side}"

    def _kalshi_rest_depth_str(self, ticker: str, side: str, ask: float, now: float) -> str:
        """The AUTHORITATIVE Kalshi depth for a diagnostic row, as a self-dating string from the
        last fire-path REST orderbook cached in `_rest_depth_cache` (fillable-at-ask). NEVER the WS
        `get_depth` value — a ticker-channel ask with no real book is the phantom we measure, so
        logging it as depth would be the bug; and a blank tells us nothing, so every case carries
        an explicit signal. Read-only, no network I/O (callers run on the hot/log path):

          fresh snapshot (<30s)       → bare fillable        ("150")
          older snapshot (≥30s)       → value tagged w/ age  ("150@47s")
          no snapshot for this ticker → "no_snapshot"        (edge died before the fire-path fetch)

        A real 0-fillable still logs "0" (a measured-empty book), distinct from "no_snapshot"
        (never measured). Shared by `_log_reject` and the edge_freshness tightest logger so the two
        diagnostics can't drift."""
        hit = getattr(self, "_rest_depth_cache", {}).get(ticker)
        if not hit:
            return "no_snapshot"
        fillable = _fillable_from_book(hit[1], side, ask)
        age = now - hit[0]
        return f"{fillable:.0f}" if age < 30.0 else f"{fillable:.0f}@{age:.0f}s"

    def _log_reject(self, opp, reason: str, detail: str = "") -> None:
        """Record a detected edge that did NOT fire, with the reason (logs/rejected_edges.csv) —
        the complete rejection ledger: below_min_edge, dominated_by_better_direction, paused,
        not_persistent, cooldown, exposure_cap, and the execution gates (band/suspect/stale/
        no-exit/thin-depth/poly-not-fillable). Throttled per (event:ticker:side:reason).
        Each row is self-contained — both legs' freshness (poly_age_s / kalshi_age_s) and
        depth (poly_depth = WS ask_depth; kalshi_depth = AUTHORITATIVE REST fillable, never the
        WS ticker value) ride alongside the reason, so a reject can be audited for frozen-feed
        artifacts without joining other logs. kalshi_depth is never blank: a fresh snapshot logs
        the bare fillable, an older one tags the value with its age ("150@47s"), and an edge that
        died before the fire-path REST fetch ran logs "no_snapshot".
        Observability ONLY — does not change what gets rejected; makes every non-fire auditable
        offline (flood of one reason = a signal, not a bug)."""
        key = f"{opp.poly_event_id}:{opp.kalshi_ticker}:{opp.kalshi_side}:{reason}"
        now = time.time()
        if now - self._last_reject_log.get(key, 0.0) <= _REJECT_LOG_THROTTLE_S:
            return
        self._last_reject_log[key] = now
        # Pull both legs' freshness + WS depth from the SAME feed detection used. Read-only,
        # and wrapped so a feed hiccup or a not-yet-primed slug can never raise into the
        # detection path — reject logging must never break the thing it observes.
        poly_age = poly_depth = kalshi_age = kalshi_depth = ""
        try:
            _pf = self._poly_feed_adapter if getattr(self, "_poly_us", False) else getattr(self, "feed", None)
            _pd = _pf.get_price(opp.poly_token) if _pf is not None else None
            if _pd is not None:
                poly_age = f"{now - _pd.last_updated:.1f}"
                poly_depth = f"{_pd.ask_depth:.0f}"
            _kf = getattr(self, "kalshi_feed", None)
            if _kf is not None:
                _ka = _kf.get_age(opp.kalshi_ticker)
                if _ka is not None:
                    kalshi_age = f"{_ka:.1f}"
                # AUTHORITATIVE REST fillable-at-ask (never the WS phantom); shared self-dating
                # sentinel logic — see _kalshi_rest_depth_str.
                kalshi_depth = self._kalshi_rest_depth_str(
                    opp.kalshi_ticker, opp.kalshi_side, opp.kalshi_ask, now)
        except (AttributeError, TypeError, ValueError, KeyError):
            pass  # leave any unresolved field blank; never block the reject row
        self._reject_csv.writerow([
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            opp.event_title, opp.poly_team, opp.poly_token,
            f"{opp.poly_ask_raw:.4f}", poly_age, poly_depth,
            opp.kalshi_ticker, opp.kalshi_side, f"{opp.kalshi_ask:.4f}", kalshi_age, kalshi_depth,
            f"{opp.edge:.6f}", opp.shares, reason, detail,
        ])

    async def _execute_kalshi_arb(self, opp: KalshiArbOpportunity) -> None:
        """Execute both legs of a Kalshi arb concurrently with stranded-leg cleanup."""
        label = f"{opp.event_title} | {opp.poly_team} vs {opp.kalshi_team}"

        # ── Implausible-edge (cross-venue PRICE-divergence) sanity ceiling ────────────
        # A real cross-venue arb is a SMALL edge — the two venues price the same outcome to
        # within a few points. opp.edge = |poly+kalshi−1| IS the implied cross-venue PRICE
        # divergence (distinct from the settlement-divergence void tail referenced below). A
        # huge edge means one venue's price is stale/wrong — e.g. a freeze-RECOVERY republish:
        # fresh transactTime, stale price → 70% phantom (would_fire wf 44/45, 2026-06-25). This
        # gate is SYMPTOM-keyed: it catches the absurd-edge phantom regardless of mechanism,
        # where the timestamp-keyed gates (G1 origin-freeze, G2 age-exclusion) cannot. Runs
        # FIRST — before any feed read and before _log_fill_success — so a phantom is recorded
        # as a reject but never enters fill_success / would_fire / the verdict. Fail-direction:
        # toward NOT firing (a real >max edge — never observed; largest seen ≈0.33 — is a missed
        # opportunity, not capital). Threshold is empirical-with-headroom, NOT a proven bound.
        if opp.edge > config.KALSHI_MAX_PLAUSIBLE_EDGE:
            self._log_reject(opp, "implausible_edge",
                             f"edge={opp.edge:.4f} > max={config.KALSHI_MAX_PLAUSIBLE_EDGE} "
                             f"(cross-venue price divergence — phantom, e.g. freeze-recovery)")
            return

        # ── Extreme-price band gate (floor-pathology guard, NOT the divergence gate) ──
        # The divergence tail is gated by KALSHI_ARB_MIN_EDGE (a bad divergence loses
        # ~the full stake at any price, so break-even ≈ edge). This band does a NARROWER
        # job: exclude price-floor pathologies near 0/1 — Kalshi's per-contract fee
        # rounding to ~100% of a 1¢ leg, thin/auction-y books, asymmetric LFMP void
        # recovery — and the strand asymmetry where a cheap-leg miss strands the
        # expensive one. Bound both legs to [floor, 1-floor]. No feed reads needed.
        _lo = config.KALSHI_MIN_POLY_PRICE
        _hi = 1.0 - _lo
        if not (_lo <= opp.poly_ask_raw <= _hi and _lo <= opp.kalshi_ask <= _hi):
            self._log_reject(opp, "extreme_band",
                             f"poly={opp.poly_ask_raw:.3f} kalshi={opp.kalshi_ask:.3f} band=[{_lo},{_hi:.2f}]")
            return

        # ── Book-trust gate ───────────────────────────────────────────────────
        # Refuse to trade a ticker whose book is untrustworthy (recent seq gap,
        # REST divergence, or resnapshot in flight). This is the gate that would
        # have blocked the stale-book loss — the book was visibly broken for minutes.
        if self.kalshi_feed.is_suspect(opp.kalshi_ticker):
            now = time.time()
            if now - self._last_stale_warn.get(opp.poly_event_id, 0.0) > 60.0:
                self._last_stale_warn[opp.poly_event_id] = now
                log.warning(
                    f"Kalshi arb skip: book suspect (recent gap/divergence/resnapshot) "
                    f"on {opp.kalshi_ticker}"
                )
            self._log_reject(opp, "book_suspect", f"ticker={opp.kalshi_ticker}")
            return

        # ── Stale-price gate ──────────────────────────────────────────────────
        # If either cached price is too old, the market may have moved and the
        # arb may no longer exist. Skip rather than send orders into stale data.
        max_age = config.KALSHI_MAX_PRICE_AGE_SECONDS
        _poly_feed_e = self._poly_feed_adapter if self._poly_us else self.feed
        _p_data = _poly_feed_e.get_price(opp.poly_token)
        poly_age = (time.time() - _p_data.last_updated) if _p_data else float("inf")
        kalshi_age = self.kalshi_feed.get_age(opp.kalshi_ticker) or float("inf")
        if poly_age > max_age or kalshi_age > max_age:
            # Throttle per event: a dead feed re-fires this every tick, and the
            # ever-incrementing age defeats the Discord dedup → 429 flood.
            now = time.time()
            if now - self._last_stale_warn.get(opp.poly_event_id, 0.0) > 60.0:
                self._last_stale_warn[opp.poly_event_id] = now
                log.warning(
                    f"Kalshi arb skipped (stale prices): {label} "
                    f"poly_age={poly_age:.0f}s kalshi_age={kalshi_age:.0f}s max={max_age}s"
                )
            self._log_reject(opp, "stale_price",
                             f"poly_age={poly_age:.0f}s kalshi_age={kalshi_age:.0f}s max={max_age}s")
            return
        age_tag = f"poly_age={poly_age:.1f}s kalshi_age={kalshi_age:.1f}s"

        # Both limits from the ONE definition — see fire_limits, which owns the reasoning for each
        # (why the Kalshi leg floors to a tick and carries the whole buffer, why the Poly leg bids
        # the ask flat). Do not re-derive either one here.
        #
        # This used to re-derive both inline, byte-identical to fire_limits. The extraction only
        # half-landed: the detect-probe was repointed at fire_limits and the fire path was not,
        # which REVERSED the drift it was extracted to prevent — fire_limits' docstring promises
        # "a probe that derives limits its own way measures a bot we don't run", and the probe was
        # the one holding the copy. Its output feeds detect_probe.csv and the S11 decision on
        # whether to delete the 0.3s persistence gate, so a change here would have silently left
        # that decision resting on the old bot's prices. Two corrections to the buffer's meaning
        # have since had to chase this call site down; both would have been one edit if the
        # reasoning had lived in one place from the start. test_detect_probe.py pins that
        # structurally now — it asserts both users call fire_limits and that neither re-derives.
        poly_limit, kalshi_limit = fire_limits(opp)
        kalshi_price_str = f"{kalshi_limit:.4f}"

        # Exit-liquidity gate: if Kalshi has no resting bid on our side, a
        # sell-back can't fill — so if the Poly leg then misses we'd strand the
        # Kalshi leg. Skip up front. Uses the cached bid (zero latency); it's a
        # price-existence proxy, not a size guarantee, but it catches the
        # empty-book case that caused the strands.
        exit_bid = self.kalshi_feed.get_best_bid(opp.kalshi_ticker, opp.kalshi_side)
        if not exit_bid or exit_bid <= 0.0:
            log.warning(
                f"Kalshi arb skip: no exit liquidity (bid={exit_bid}) on "
                f"{opp.kalshi_ticker} [{opp.kalshi_side}] — would strand if Poly misses"
            )
            self._log_reject(opp, "no_exit_liquidity", f"bid={exit_bid}")
            return

        # ── Fire-time REST validation: BOTH legs in PARALLEL ──────────────────────────
        # The Kalshi entry-depth snapshot and the Poly book quote are independent REST
        # calls to different venues → gather them so the two RTTs overlap, halving the
        # fire-path validation latency. Both are internally fail-closed (never raise), so
        # the gather can't surface an exception. Gate CONDITIONS and their ORDER below are
        # UNCHANGED — only the fetch is concurrent (on a tick where the entry-depth gate
        # would have short-circuited, we now also make the cached Poly call; both 1s-cached).
        poly_transact: str | None = None
        poly_read_ms: float | None = None
        # Pre-initialised like the two above because it is read at method level (the size-curve
        # log) but only ASSIGNED inside this `if` — on the legacy non-poly_us path it would
        # otherwise be unbound.
        poly_levels: list[tuple[float, float]] | None = None
        if self._poly_us:
            entry_depth, poly_result = await asyncio.gather(
                self._rest_fillable(opp.kalshi_ticker, opp.kalshi_side, kalshi_limit),
                self._poly_fill_quote(opp.poly_token),
            )
            poly_quote, poly_read_ms = poly_result
            live_poly_ask, poly_state, poly_levels, poly_transact, poly_stats = poly_quote
        else:
            entry_depth = await self._rest_fillable(opp.kalshi_ticker, opp.kalshi_side, kalshi_limit)

        # ── Size to what the KALSHI book can actually fill (fresh REST, authoritative) ─────────
        # Sizing ran at DETECTION off the WS cache and never saw the Kalshi book — it capped on
        # Poly depth + balances only, on the premise "Kalshi fills any size at its quote". Kalshi
        # is a CLOB; that premise is a same-platform-era leftover. Fresh REST is authoritative (the
        # WS book can FREEZE on a live game — it held a phantom 0.13 NO for 7 min) and works in
        # ticker mode. Fail-closed: any read error → 0 → reject.
        #
        # This REPLACES a binary `entry_depth < opp.shares → reject` gate. That was right under FOK
        # (no depth → 0 fill → the Poly leg strands), but both legs are IOC now: we fill whatever is
        # there. Measured: on 87/507 (17%) of kalshi_moved events the book holds PARTIAL depth
        # (median 3 vs a median-7 target) — the gate rejected exactly the population the FOK→IOC
        # swap exists to capture. Size down instead; only a book that can't fill even ONE share is
        # a real reject.
        # ⚠️ Deliberately BEFORE _log_fill_success, so the dataset records what we would ACTUALLY
        # fire. This REDEFINES kalshi_moved (partial-depth events now both_fill) — a regime break:
        # do not pool fill_success rows across 2026-07-15. See docs/TODO.md S2.
        if entry_depth >= 1 and entry_depth < opp.shares:
            log.info(
                f"Kalshi arb: sizing {opp.shares} → {int(entry_depth)} to the Kalshi book at limit "
                f"{kalshi_limit:.4f} on {opp.kalshi_ticker} [{opp.kalshi_side}]"
            )
            resize_opportunity(opp, int(entry_depth))

        if self._poly_us:
            # Fill-success observability: both legs' fresh-REST fillability at the re-read, BEFORE
            # any gate short-circuits. Side-effect only, fail-safe — does not alter firing.
            # ⚠️ MUST stay ahead of the entry_depth reject below. It sits AFTER the resize (so the
            # row records the size we would ACTUALLY fire) but BEFORE the reject — an
            # entry_depth<1 return that skipped this would silently drop the EMPTY-KALSHI-BOOK
            # population from the dataset, historically 420/507 of kalshi_moved and its single
            # biggest failure class. Losing it inflates both_fill by shrinking the denominator —
            # self-flattering, in exactly the number the go-live verdict rests on. (That regression
            # shipped on 2026-07-15 and was caught the same day; don't reintroduce it by moving
            # this line.)
            self._log_fill_success(opp, poly_limit, kalshi_limit, entry_depth,
                                   live_poly_ask, poly_state, poly_levels, poly_read_ms, poly_transact)

        if entry_depth < 1:
            now = time.time()
            if now - self._last_stale_warn.get(opp.poly_event_id, 0.0) > 60.0:
                self._last_stale_warn[opp.poly_event_id] = now
                log.warning(
                    f"Kalshi arb skip: no entry depth ({entry_depth:.0f}) buyable at limit "
                    f"{kalshi_limit:.4f} on {opp.kalshi_ticker} [{opp.kalshi_side}] — "
                    f"Kalshi would 0-fill and strand Poly to unwind"
                )
            self._log_reject(opp, "thin_entry_depth",
                             f"entry_depth={entry_depth:.0f} < 1 @limit={kalshi_limit:.4f}")
            return

        # Fire-time Poly freshness + market-state re-check (book quote fetched above).
        #   • STATE: after a goal Poly SUSPENDS the market, freezing a stale price that
        #     is NOT tradeable — it manufactures huge fake edges (a 40%+ "arb" that's a
        #     halt artifact). Firing into a suspended book rejects or fills post-unhalt
        #     at a bad price. Only fire when MARKET_STATE_OPEN.
        #   • PRICE: a stale-but-open feed quote vs a fresh Kalshi quote also makes
        #     phantoms; re-validate against the live book.
        # Authoritative vs feed age/state; fail-closed (None / not-open / moved → skip).
        rest_poly_fillable: float | None = None
        if self._poly_us:
            # A book that returned NO state field is an API/schema hiccup, NOT a real halt.
            # Reject with a DISTINCT reason + warning so an empty would_fire from a feed
            # problem is greppable, not silently lumped with "no edges". Still fail-closed.
            if poly_state == "?":
                now = time.time()
                if now - self._last_stale_warn.get(opp.poly_event_id, 0.0) > 60.0:
                    self._last_stale_warn[opp.poly_event_id] = now
                    log.warning(
                        f"Kalshi arb skip: Poly book returned NO state field on "
                        f"{opp.poly_token} (API/schema issue, not a halt) — fail-closed. "
                        f"If this floods, would_fire is empty for a feed reason, not edges."
                    )
                self._log_reject(opp, "poly_state_unavailable",
                                 f"book carried no state field; live_ask={live_poly_ask}")
                return
            if poly_state != "MARKET_STATE_OPEN" or live_poly_ask is None or live_poly_ask > poly_limit:
                now = time.time()
                if now - self._last_stale_warn.get(opp.poly_event_id, 0.0) > 60.0:
                    self._last_stale_warn[opp.poly_event_id] = now
                    log.warning(
                        f"Kalshi arb skip: Poly leg not fillable at fire — "
                        f"state={poly_state} live_ask={live_poly_ask} limit={poly_limit:.4f} "
                        f"on {opp.poly_token} (suspended/stale → would strand the Kalshi leg)"
                    )
                self._log_reject(opp, "poly_not_fillable",
                                 f"state={poly_state} live_ask={live_poly_ask} limit={poly_limit:.4f}")
                return
            # Fillable AT the price we'll pay — sum qty across all ask levels <= poly_limit
            # (spans levels; authoritative REST, symmetric with kalshi entry_depth at limit).
            rest_poly_fillable = sum(q for p, q in poly_levels if p <= poly_limit)

        # Poly fillable at fire: authoritative REST (us mode, from the fire-time quote above
        # — same book fetch, no extra round-trip) instead of the freeze-prone WS cache. The
        # global (dormant) path keeps the WS read.
        if rest_poly_fillable is not None:
            poly_fillable = rest_poly_fillable
        else:
            _pd_e = _poly_feed_e.get_price(opp.poly_token)
            poly_fillable = getattr(_pd_e, "ask_depth", 0.0) if _pd_e else 0.0

        # ── Origin-freeze gate (G1) — CONTENT-freshness, not read-recency ────────────
        # A fresh REST read (cache-busted, cf=MISS) can STILL return an ORIGIN-stale book: Poly's
        # origin served a 186–373s-old transactTime while state=OPEN during a live game (the
        # 2026-06-25 00:39–00:43Z burst that manufactured the largest phantom edges, all of which
        # passed every gate above). The prior gates don't catch it — the stale_price gate is
        # READ-recency (time since we last read), not the book's server transactTime, and state was
        # OPEN. Gate on the server transactTime here, BEFORE _log_would_fire, so a frozen book never
        # FIRES and never enters would_fire.csv. (It IS still logged to fill_success.csv — that write
        # happens earlier, at the book re-read above, before any gate, so fill_success records the raw
        # fire-time outcome incl. frozen ones with rest_transact_age_s>30. The interlock: G2's verdict
        # EXCLUDES those age>30 rows by this SAME 30s boundary, so log → fire-decision → verdict all
        # agree — this gate stops the fire, G2 keeps the frozen row out of the denominator.)
        #
        # rest_transact_age_s ALONE is ambiguous (frozen vs a legitimately-illiquid book that just
        # didn't tick — the §5 / money-path-review #5 trap), so it is a FLAG, not a verdict. The
        # non-ambiguous discriminator is CROSS-MARKET: an origin freeze stalls MANY books'
        # transactTime at the same instant (four unrelated games shared one freeze-epoch in the
        # burst), which a single illiquid book cannot. Reject ONLY when this book is freeze-suspect
        # AND ≥ config.ORIGIN_FREEZE_MIN_PEERS other tracked markets are simultaneously stale; a LONE
        # stale book is NOT rejected (preserve the real illiquid-edge tail). FAIL-SAFE: a missing /
        # unparseable transactTime → age None → not suspect → the gate never MANUFACTURES a fire and
        # never falsely rejects. Threshold is the SHARED config.FROZEN_BOOK_AGE_S — the same boundary
        # the verdict (G2) excludes on, so "fired" and "counted" can never disagree.
        if self._poly_us:
            fire_transact_age = transact_age_s(poly_transact, time.time())
            # Cheap Stage-1 flag: only scan the slate (Stage 2) when THIS book is itself stale.
            if fire_transact_age is not None and fire_transact_age > config.FROZEN_BOOK_AGE_S:
                # Scope the peer scan to LIVE-game markets — an upcoming/finished game in the
                # discovery slate is legitimately quiet (old transactTime) and must NOT count as a
                # freeze peer (else a quiet period false-confirms a freeze on a real illiquid edge).
                now_s = time.time()
                peers_stale = self._poly_us_feed.count_stale_books(
                    now_s, config.FROZEN_BOOK_AGE_S, exclude_token=opp.poly_token,
                    in_window=in_window_slugs(getattr(self, "active_pairs", []), now_s))
                if _origin_freeze_reject(fire_transact_age, peers_stale,
                                         config.FROZEN_BOOK_AGE_S, config.ORIGIN_FREEZE_MIN_PEERS):
                    now = time.time()
                    if now - self._last_stale_warn.get(opp.poly_event_id, 0.0) > 60.0:
                        self._last_stale_warn[opp.poly_event_id] = now
                        log.warning(
                            f"Kalshi arb skip: ORIGIN-FREEZE suspected on {opp.poly_token} — book "
                            f"transactTime {fire_transact_age:.0f}s stale AND {peers_stale} other "
                            f"market(s) simultaneously stale (cross-market freeze, not illiquidity) "
                            f"— firing would hit a frozen book (phantom edge)."
                        )
                    self._log_reject(opp, "origin_freeze_suspect",
                                     f"book_age={fire_transact_age:.0f}s peers_stale={peers_stale} "
                                     f"thr={config.FROZEN_BOOK_AGE_S:.0f}s")
                    return

        # ── Phase 1.5 would-fire log ──────────────────────────────────────────
        # This opp passed EVERY gate (settlement, persistence, min-price, book-trust,
        # stale, exit-liquidity, entry-depth, origin-freeze). Record it + sample the book
        # evolution for the settlement backtest — in BOTH dry and live so DRY accumulates data.
        transact_age = transact_age_s(poly_transact, time.time())
        self._log_would_fire(opp, poly_limit, kalshi_limit, poly_fillable, entry_depth,
                             transact_age, poly_read_ms, poly_stats, poly_levels)

        if config.DRY_RUN:
            log.info(
                f"[DRY RUN] Kalshi arb: {label} | "
                f"poly={opp.poly_ask:.3f} kalshi_{opp.kalshi_side}={opp.kalshi_ask:.3f} "
                f"edge={opp.edge*100:.2f}% shares={opp.shares} "
                f"profit=${opp.guaranteed_profit:.2f} [{age_tag}]"
            )
            # (No edge_freshness.csv row here — it duplicated would_fire.csv. The freshness
            # snapshots written by the tightest logger still populate edge_freshness for
            # scripts/explain_edge.py.)
            # §5 inter-leg-window probe (DRY-only): off the hot path, never fires. Measures whether
            # the Kalshi leg would drop below `shares` in the poly-fill→kalshi-fire window — the
            # input that gates the poly_first-vs-kalshi_first decision (see docs/TODO.md §5).
            asyncio.create_task(
                self._probe_interleg_window(opp, kalshi_limit, entry_depth, poly_fillable))
            # Exec-order probe (DRY-only, off the hot path): unwind costs both venues +
            # the kalshi_first mirror of §5. Feeds the poly_first-vs-kalshi_first decision.
            asyncio.create_task(
                self._probe_exec_order(opp, poly_limit, kalshi_limit, poly_fillable))
            self.tracker.mark_traded(self._direction_key(opp))
            return

        # Past every gate — placing both legs now (logged here, not before the gates,
        # so a persistently-gated edge doesn't spam this line every WS tick).
        log.info(
            f"Kalshi arb: {label} | "
            f"poly_ask={opp.poly_ask_raw:.4f}(limit={poly_limit:.4f}) "
            f"kalshi_{opp.kalshi_side}={opp.kalshi_ask:.4f}(limit={kalshi_limit:.4f}) "
            f"edge={opp.edge*100:.2f}% shares={opp.shares} [{age_tag}]"
        )

        # Sequential dispatch. The exec helper places the legs in the safe order,
        # sizes the second leg to the actual first fill, and unwinds/records.
        if config.KALSHI_EXEC_ORDER == "kalshi_first":
            await self._exec_kalshi_first(opp, poly_limit, kalshi_limit, kalshi_price_str, poly_fillable, label)
        else:
            await self._exec_poly_first(opp, poly_limit, kalshi_limit, kalshi_price_str, poly_fillable, label)

        # Lock this DIRECTION out so it can't immediately re-fire (other directions on
        # the same game stay eligible).
        self.tracker.mark_traded(self._direction_key(opp))

    def _record_hedge(self, opp, qty: int, poly_limit: float, kalshi_limit: float, label: str,
                      poly_cost: float | None = None, kalshi_cost: float | None = None) -> None:
        """Record a hedged pair of `qty` contracts: position, buying-power decrement,
        and the EXECUTED trade log. Shared by both execution orders.

        `poly_cost` / `kalshi_cost` are the ACTUAL fee-inclusive costs per share, read from the
        venues' own fill reports (kalshi_avg_fill_cost / poly_avg_fill_cost). Book off those, not
        the DETECTION price: both legs are IOC and can sweep several levels to a breakeven-wide
        limit, so the realized cost can be materially worse than `opp.*_ask`. This is not merely a
        marked_unsettled cosmetic — `cost` below feeds trades.log, which settlement_scorer reads to
        compute realized_settled, the sizing authority the loss cap acts on.

        None = the venue's fill report was unreadable → fall back to the detection price (the old
        behaviour) and say so loudly. The fallback is OPTIMISTIC, so it must never pass silently.
        """
        p_eff = poly_cost if poly_cost is not None else opp.poly_ask
        # NOT opp.kalshi_ask — that is the RAW ask and the fallback would be fee-FREE,
        # ~1.7c/share short (see _kalshi_effective). opp.poly_ask above is already effective.
        k_eff = kalshi_cost if kalshi_cost is not None else _kalshi_effective(opp)
        if poly_cost is None or kalshi_cost is None:
            log.warning(
                f"{label} | booking off DETECTION prices — actual fill cost unreadable "
                f"(poly={poly_cost!r} kalshi={kalshi_cost!r}). realized_settled will be optimistic "
                f"by however far the fills slipped."
            )
        cost = (p_eff + k_eff) * qty
        profit = qty * 1.0 - cost
        self.tracker.add_position(
            opp.poly_event_id, opp.event_title,
            opp.poly_token,    p_eff * qty,   float(qty),
            opp.kalshi_ticker, k_eff * qty,   float(qty),
        )
        if not config.DRY_RUN:
            self._poly_buying_power = max(0.0, self._poly_buying_power - opp.poly_ask_raw * qty)
            self._kalshi_buying_power = max(0.0, self._kalshi_buying_power - k_eff * qty)
        log.info(f"Kalshi arb filled: {label} hedged={qty}/{opp.shares} profit=${profit:.2f}")
        log_trade({
            "type": "kalshi-arb", "event": opp.event_title, "event_id": opp.poly_event_id,
            "poly_team": opp.poly_team, "poly_token": opp.poly_token,
            "poly_ask_raw": opp.poly_ask_raw, "poly_ask_eff": p_eff, "poly_limit": poly_limit,
            "kalshi_ticker": opp.kalshi_ticker, "kalshi_side": opp.kalshi_side,
            "kalshi_team": opp.kalshi_team, "kalshi_ask": k_eff, "kalshi_limit": kalshi_limit,
            # detection-time, kept for slippage analysis against the actual above
            "poly_ask_eff_detect": opp.poly_ask, "kalshi_ask_detect": opp.kalshi_ask,
            "shares": qty, "cost": cost, "guaranteed_profit": profit,
            "edge": round(opp.edge, 6), "status": "EXECUTED",
        })
        # Booked (not yet realized — a void can erode it). Tracked separately from
        # execution_cost so the kill never nets against optimistic edge.
        self._exec_pnl_csv.writerow([
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "marked_unsettled", "hedge", opp.event_title, opp.kalshi_ticker, qty,
            f"{profit:.4f}", "",
        ])

    async def _place_poly(self, opp, poly_limit: float, qty: int) -> tuple[int, object]:
        """Place the Poly leg (price-capped, NOT marketable) for `qty`. Returns
        (ACTUAL filled quantity, raw response).

        The qty is read from the response, so the sequence is correct given Poly is IOC and can
        partial-fill. The RESPONSE is returned too because it carries avgPx +
        commissionNotionalTotalCollected — the ACTUAL fee-inclusive cost _record_hedge books off
        (it used to be discarded, so P&L was booked at the detection price)."""
        lbl = f"[KALSHI-ARB] {opp.poly_team} YES"
        if self._poly_us:
            resp = await self.client.place_limit_fok(opp.poly_token, poly_limit, float(qty), lbl)
            return int(self._order_filled_qty(resp)), resp
        resp = await asyncio.to_thread(self.client.place_limit_fok, opp.poly_token, poly_limit, float(qty), lbl)
        # Legacy client: no fill report → poly_avg_fill_cost(resp) returns None → caller falls back.
        return (qty if (resp is not None and not isinstance(resp, Exception)) else 0), resp

    async def _exec_poly_first(self, opp, poly_limit, kalshi_limit, kalshi_price_str, poly_fillable, label) -> None:
        """Liquid leg first: fire Poly capped, then the Kalshi FOK leg sized to the
        ACTUAL Poly fill. A Kalshi miss unwinds the Poly leg (cheap, liquid) — it can
        never strand the illiquid Kalshi leg."""
        try:
            P, poly_resp = await self._place_poly(opp, poly_limit, opp.shares)
        except Exception as e:
            # AMBIGUOUS: the Poly order errored, so we may hold up to opp.shares and cannot tell.
            # Only timeout / 502 / reset reach here — a real IOC kill is a 200 with zero filled —
            # and `synchronousExecution` blocks the order ~61ms server-side, so a timeout lands
            # INSIDE the fill window. This used to be indistinguishable from a clean miss:
            # place_limit_fok swallowed the error and returned None, which became P=0 and took the
            # branch below, alerting "MISSED (no fill)" under a comment reading "no exposure".
            #
            # Nothing else has fired (Poly is the first leg), so there is no second leg at risk and
            # nothing to unwind — the same position _exec_kalshi_first is in when ITS first leg
            # errors, and handled the same way: do NOT guess a qty, because recording a position we
            # may not hold is its own harm, and it would book a phantom cost into the loss cap.
            # Stop here; the reconciler asks Poly what we actually hold and strands + pauses on a
            # real leg within ~2 polls. That backstop is why this is loud rather than paused —
            # and reconcile.py's own docstring names THIS failure as its reason for existing.
            log.critical(
                f"Poly order FAILED before any Kalshi order — leg state UNKNOWN (we may hold up "
                f"to {opp.shares} of {opp.poly_token}): {e!r}. Not firing Kalshi; reconciler will "
                f"surface any real position."
            )
            fills_log.info(f"{label} | POLY-FIRST poly_order_FAILED={e!r}")
            self._alert_miss(opp)
            return
        if P <= 0:
            # The venue ANSWERED and filled nothing — a real IOC kill, not an unknown. Nothing
            # else fired, so there genuinely is no exposure here.
            self._alert_miss(opp)
            return
        # Kalshi IOC for EXACTLY the Poly fill P (sized from actual fill, never opp.shares).
        #
        # IOC, not FOK — deliberate [decided 2026-07-15 from fill_success.csv, 507 kalshi_moved
        # economic events]. Poly has ALREADY filled here, so the only question is how much of it we
        # can hedge:
        #   420/507 (83%): the Kalshi book is EMPTY at our limit → FOK and IOC both fill 0.
        #                  IDENTICAL. No downside to IOC.
        #    87/507 (17%): the book holds PARTIAL depth → they diverge, and FOK is strictly worse:
        #                  FOK kills  → hedge 0,   flatten ALL 645 Poly shares, capture 0
        #                  IOC fills  → hedge 218, flatten only 427 (-34%),     capture 218
        # Safe because an IOC fills only at prices <= kalshi_price_str, and that limit IS the
        # breakeven ask (_kalshi_breakeven_ask) — so every filled share is profitable. Same edge
        # floor, more of it captured. A partial kfill is already reconciled below (hedged =
        # min(kfill,P); the P-kfill Poly excess is flattened).
        try:
            kalshi_resp = await self.kalshi_client.create_order(
                opp.kalshi_ticker, opp.kalshi_side, "buy", P, kalshi_price_str,
                time_in_force="immediate_or_cancel",
            )
        except Exception as e:
            # AMBIGUOUS: Poly is filled and the Kalshi leg is UNKNOWN — a timeout/502/reset may
            # or may not have reached the engine. Do NOT guess; both guesses strand the other leg:
            #   assume kfill=0 → flatten Poly → if it DID fill we are naked KALSHI + a flatten cost
            #   assume filled  → do nothing  → if it did NOT we are naked POLY
            # Strand what we KNOW we hold (P Poly) → loud alert + global pause; the reconciler
            # then asks both venues what we actually hold and surfaces any Kalshi leg (~2 polls).
            # Without this the exception escaped the helper entirely and _unwind_poly_excess never
            # ran — a naked, UNRECORDED Poly leg with no strand, no pause, no alert.
            # (A 409 FOK-kill is NOT this case: create_order returns a zero-fill for it, so the
            # normal kfill==0 flatten below still runs.)
            log.critical(
                f"Kalshi order FAILED after Poly filled {P} — state UNKNOWN (leg may or may not "
                f"have filled): {e!r}. Stranding the Poly leg and pausing; reconciler will resolve."
            )
            fills_log.info(f"{label} | POLY-FIRST poly_fill={P} kalshi_order_FAILED={e!r}")
            self.tracker.add_stranded(
                opp.poly_event_id, opp.event_title, opp.poly_token, float(P), opp.poly_ask * P,
            )
            self._record_exec_cost(
                "strand_poly", opp, P, opp.poly_ask * P, f"kalshi order failed: {type(e).__name__}",
            )
            send_kalshi_arb_alert(
                opp, poly_filled=True, kalshi_filled=False, is_dry_run=config.DRY_RUN,
            )
            return
        kfill = int(kalshi_filled_qty(kalshi_resp))
        fills_log.info(
            f"{label} | POLY-FIRST poly_fill={P}/{opp.shares} kalshi_fok={kfill}/{P} "
            f"poly_limit={poly_limit:.4f} kalshi_limit={kalshi_price_str} | kalshi_resp={kalshi_resp!r}"
        )
        # Reconcile against the ACTUAL Kalshi fill, symmetric with _exec_kalshi_first.
        # FOK on a P-sized order should fill exactly P or kill (0), so kfill == P or
        # kfill == 0 are the only expected outcomes. But this whole branch exists because
        # the FOK might NOT behave — and a misbehaving FOK can under-fill OR over-fill, so
        # we defend both directions rather than assume one away:
        #   under-fill (0 < kfill < P): hedge kfill, flatten the P − kfill Poly excess.
        #     (Flattening all P would leave kfill Kalshi naked + unrecorded — a silent strand.)
        #   over-fill  (kfill > P):     hedge P, unwind the kfill − P Kalshi excess — the
        #     mirror strand, exactly how _exec_kalshi_first handles its excess_kalshi.
        hedged = min(kfill, P)
        if hedged > 0:
            self._record_hedge(opp, hedged, poly_limit, kalshi_limit, label,
                               poly_cost=poly_avg_fill_cost(poly_resp),
                               kalshi_cost=kalshi_avg_fill_cost(kalshi_resp))
        poly_excess = P - hedged       # Poly we couldn't hedge (under-fill / Kalshi kill)
        if poly_excess > 0:
            log.error(
                f"Kalshi arb: Poly filled {P}, Kalshi FOK filled {kfill} — flattening "
                f"{poly_excess} unhedged Poly"
            )
            await self._unwind_poly_excess(opp, poly_excess, label,
                                           entry_cost=poly_avg_fill_cost(poly_resp))
        kalshi_excess = kfill - hedged  # Kalshi beyond our Poly hedge (FOK over-fill)
        if kalshi_excess > 0:
            log.error(
                f"Kalshi arb: Kalshi FOK over-filled {kfill} vs Poly {P} — unwinding "
                f"{kalshi_excess} excess Kalshi"
            )
            await self._unwind_kalshi_excess(opp, kalshi_excess, label,
                                             entry_cost=kalshi_avg_fill_cost(kalshi_resp))
        send_kalshi_arb_alert(
            opp, poly_filled=True, kalshi_filled=(kfill > 0), is_dry_run=config.DRY_RUN,
        )

    async def _exec_kalshi_first(self, opp, poly_limit, kalshi_limit, kalshi_price_str, poly_fillable, label) -> None:
        """Uncertain leg first: Kalshi FOK (zero cost if it kills), then complete Poly
        sized to the Kalshi fill. Fewer round-trips but a residual strand if Poly then
        misses — for the live flatten-vs-strand A/B only."""
        # IOC, not FOK — deliberate [decided 2026-07-15]. Under FOK, kfill ∈ {0, opp.shares}: a
        # book holding only 3 of our 8 KILLS and we capture nothing. Under IOC it fills the 3, and
        # the Poly leg below is then sized to that ACTUAL 3 — a small arb instead of none. Safe:
        # an IOC fills only at prices <= kalshi_price_str, which IS the breakeven ask, so every
        # filled share is profitable.
        #
        # ⚠️ This makes a PARTIAL kfill reachable for the first time (FOK made it impossible), so
        # the "size the second leg to the first leg's ACTUAL fill" invariant now has teeth here:
        # _place_poly is called with kfill, never opp.shares. Pinned in tests.
        # It also softens kalshi_first's "kill = free stop": a partial fill COMMITS us to kfill.
        # That is the right trade — on the 87 partial-depth events Poly would have filled, so we
        # capture the hedge; and when Poly does miss, unwinding kfill (<= shares) is CHEAPER than
        # unwinding a full FOK fill.
        try:
            kalshi_resp = await self.kalshi_client.create_order(
                opp.kalshi_ticker, opp.kalshi_side, "buy", opp.shares, kalshi_price_str,
                time_in_force="immediate_or_cancel",
            )
        except Exception as e:
            # Kalshi fired FIRST, so no Poly order exists — we hold no Poly either way. But the
            # Kalshi leg is UNKNOWN (a timeout may have reached the engine), so do NOT proceed to
            # fire Poly against a fill we cannot confirm. Alert and stop; the reconciler surfaces a
            # real Kalshi leg within ~2 polls. Nothing to strand: guessing a qty here would record
            # a position we may not hold. Unguarded, this raise escaped and killed the arb loop.
            log.critical(
                f"Kalshi order FAILED (kalshi_first, before any Poly order) — leg state UNKNOWN: "
                f"{e!r}. Not firing Poly; reconciler will surface any real position."
            )
            fills_log.info(f"{label} | KALSHI-FIRST kalshi_order_FAILED={e!r}")
            self._alert_miss(opp)
            return
        kfill = int(kalshi_filled_qty(kalshi_resp))
        if kfill <= 0:
            self._alert_miss(opp)            # Kalshi filled nothing → no Poly order, no exposure
            return
        try:
            P, poly_resp = await self._place_poly(opp, poly_limit, kfill)
        except Exception as e:
            # AMBIGUOUS, and unlike the poly_first case we DO hold something: kfill of Kalshi is
            # confirmed, and the Poly leg is unknown. This is the exact mirror of _exec_poly_first's
            # Kalshi handler, so it does the same thing — strand the leg we KNOW we hold, which
            # pauses globally, and let the reconciler resolve Poly within ~2 polls.
            #
            # Do not guess Poly either way: assume it filled and we may be naked Kalshi with no
            # flatten; assume it did not and we may be naked Poly on top. The Kalshi leg is the one
            # fact we have. Before place_limit_fok raised, this could not happen — the error came
            # back as P=0, we "flattened" a Poly leg we might really hold, and booked the cost.
            log.critical(
                f"Poly order FAILED after Kalshi filled {kfill} — Poly state UNKNOWN (may or may "
                f"not have filled): {e!r}. Stranding the Kalshi leg and pausing; reconciler will "
                f"resolve Poly."
            )
            fills_log.info(f"{label} | KALSHI-FIRST kalshi_fok={kfill} poly_order_FAILED={e!r}")
            self.tracker.add_stranded(
                opp.poly_event_id, opp.event_title, opp.kalshi_ticker, float(kfill),
                opp.kalshi_ask * kfill,
            )
            self._record_exec_cost(
                "strand_kalshi", opp, kfill, opp.kalshi_ask * kfill,
                f"poly order failed: {type(e).__name__}",
            )
            send_kalshi_arb_alert(
                opp, poly_filled=False, kalshi_filled=True, is_dry_run=config.DRY_RUN,
            )
            return
        fills_log.info(
            f"{label} | KALSHI-FIRST kalshi_fok={kfill}/{opp.shares} poly_fill={P}/{kfill} "
            f"poly_limit={poly_limit:.4f} kalshi_limit={kalshi_price_str} | kalshi_resp={kalshi_resp!r}"
        )
        # Reconcile against the ACTUAL fills, symmetric with _exec_poly_first. Poly does NOT honor
        # FOK — it coerces to IOC and can partial-fill [VERIFIED poly_us/client.py:order_filled_qty]
        # — so P ∈ [0, kfill] is the expected space and P > kfill should not occur. Defend it anyway,
        # for the same reason _exec_poly_first defends its own mirror: "a misbehaving FOK can
        # under-fill OR over-fill, so we defend both directions rather than assume one away".
        #   under-fill (P < kfill): hedge P, unwind the kfill − P Kalshi left naked.
        #   over-fill  (P > kfill): hedge kfill, flatten the P − kfill Poly excess. Without this the
        #     excess is naked AND UNRECORDED — invisible to the tracker, the exposure caps and the
        #     strand alert, i.e. silent, which is worse than a strand.
        hedged = min(kfill, P)
        if hedged > 0:
            self._record_hedge(opp, hedged, poly_limit, kalshi_limit, label,
                               poly_cost=poly_avg_fill_cost(poly_resp),
                               kalshi_cost=kalshi_avg_fill_cost(kalshi_resp))
        excess_kalshi = kfill - hedged
        if excess_kalshi > 0:
            log.error(f"Kalshi arb: {excess_kalshi} unhedged Kalshi (Poly missed) — unwinding")
            await self._unwind_kalshi_excess(opp, excess_kalshi, label,
                                             entry_cost=kalshi_avg_fill_cost(kalshi_resp))
        poly_excess = P - hedged
        if poly_excess > 0:
            log.error(
                f"Kalshi arb: Poly over-filled {P} vs Kalshi {kfill} — flattening "
                f"{poly_excess} unhedged Poly"
            )
            await self._unwind_poly_excess(opp, poly_excess, label,
                                           entry_cost=poly_avg_fill_cost(poly_resp))
        send_kalshi_arb_alert(opp, poly_filled=(P > 0), kalshi_filled=True, is_dry_run=config.DRY_RUN)

    def _alert_miss(self, opp) -> None:
        """Throttled both-miss heads-up (no money moved)."""
        now = time.time()
        if now - self._last_miss_alert.get(opp.poly_event_id, 0.0) > _MISS_ALERT_THROTTLE_S:
            self._last_miss_alert[opp.poly_event_id] = now
            send_kalshi_arb_alert(opp, poly_filled=False, kalshi_filled=False, is_dry_run=config.DRY_RUN)

    async def _kalshi_price_refresh_loop(self) -> None:
        """Poll Kalshi REST every 10s. Two roles depending on the price source:

        ticker mode    — refresh cached prices so a calm market doesn't age past
                         the execution gate (kept under KALSHI_MAX_PRICE_AGE_SECONDS).
        orderbook mode — refresh() is a no-op (the book is the source of truth), so
                         instead use the same REST snapshot to AUDIT the WS book: if
                         the maintained best yes-bid has drifted from REST (a stale
                         level the delta stream left behind → phantom edges), force a
                         resnapshot. Off the hot path; no added execution latency.
        """
        if not config.KALSHI_API_KEY:
            return
        orderbook_mode = config.KALSHI_PRICE_SOURCE == "orderbook"
        last_health_log = time.time()
        while True:
            await asyncio.sleep(config.KALSHI_AUDIT_INTERVAL)
            if not self._kalshi_pairs:
                continue
            try:
                # Re-fetch all series markets (4 REST calls, not one per ticker).
                markets = await self.kalshi_scanner.fetch_markets(config.KALSHI_SERIES)
                price_map = {m.ticker: (m.yes_bid, m.yes_ask) for m in markets if m.yes_ask > 0}
                if orderbook_mode:
                    divergent = self.kalshi_feed.divergent_from_rest(
                        price_map, config.KALSHI_BOOK_MAX_DIVERGENCE
                    )
                    if divergent:
                        # Do NOT quarantine on REST divergence alone: during a LIVE game
                        # the REST markets-endpoint quote lags the real-time WS book, so
                        # divergence usually means REST is behind (WS is right) — and
                        # quarantining would block exactly the live markets we want to
                        # trade. The real staleness signal is a dropped delta = a SEQ GAP,
                        # which marks tickers suspect in _check_seq. Here we only nudge a
                        # throttled resnapshot to flush a possible orphan level + log health.
                        did_resnap = await self.kalshi_feed.resnapshot()
                        if did_resnap:
                            tk, ws_bid, rest_bid = divergent[0]
                            log.warning(
                                f"Kalshi book divergence vs REST: {len(divergent)} ticker(s) "
                                f"(e.g. {tk} ws_yes_bid={ws_bid:.2f} rest={rest_bid:.2f}) — "
                                f"resnapshotted (throttled). Likely REST lag on a live game; "
                                f"quarantine is driven by seq gaps, not this."
                            )
                    now = time.time()
                    if now - last_health_log >= 60.0:
                        elapsed = now - last_health_log
                        last_health_log = now
                        self._log_orderbook_health(divergent, elapsed)
                    continue
                refreshed = 0
                for cp in self._kalshi_pairs:
                    ticker = cp.kalshi_market.ticker
                    prices = price_map.get(ticker)
                    if prices:
                        self.kalshi_feed.refresh(ticker, *prices)
                        refreshed += 1
                log.debug(f"Kalshi prices refreshed: {refreshed}/{len(self._kalshi_pairs)}")
                # Ticker mode keeps no maintained book to audit, so the orderbook-mode health log
                # above never runs here — but the WS book-CHANGE rate (the basis for tuning the
                # freshness watchdog's _FRESHNESS_RECONNECT_S) lives in book_health() and would
                # otherwise never be surfaced in ticker (= prod) mode. Log it so the changes/min
                # tuning data actually exists. Diagnostics only; off the hot path.
                now = time.time()
                if now - last_health_log >= 60.0:
                    elapsed = max(now - last_health_log, 1e-6)
                    last_health_log = now
                    h = self.kalshi_feed.book_health()
                    chg = h.get("changes", 0)
                    orderbook_log.info(
                        f"KALSHI FEED HEALTH (ticker) | msg/s={h['msgs']/elapsed:.0f} "
                        f"changes={chg} ({chg / elapsed * 60:.0f}/min) "
                        f"duty={h['handler_s'] / elapsed * 100:.0f}%"
                    )
            except Exception as e:
                log.error(f"Kalshi price refresh error: {e}")

    def _on_kalshi_price_update(self) -> None:
        """Called synchronously by kalshi_feed on every WS price tick."""
        self._trigger_kalshi_detect()

    def _trigger_kalshi_detect(self) -> None:
        """Debounced trigger for Kalshi arb detection — safe to call from any sync context."""
        if not self._kalshi_pairs:
            return
        if not self.kalshi_feed.subscriptions_ready:
            return
        now = time.time()
        if now - self._last_kalshi_trigger < 0.01:
            return
        self._last_kalshi_trigger = now
        asyncio.create_task(self._run_kalshi_execution())

    def _confirm_persistent(self, opps: list) -> list:
        """Persistence gate: track when each opportunity (event:ticker:side) first appeared above
        the trade threshold and return only those that have held for KALSHI_CONFIRM_SECONDS.

        ⚠️ IT DOES NOT FILTER PHANTOMS. This used to claim "a sub-second goal-spike phantom never
        clears the window" — REFUTED 2026-07-15: of the edges that DO clear it, **61.2% (485/792
        economic events) are already dead when the fresh-REST read lands ~20ms later**. An edge
        cannot die in 20ms 61% of the time, so the WS price was wrong for the whole window and this
        measured a phantom's persistence.

        It cannot work mechanically: **a stale quote is MAXIMALLY persistent** — it never moves, so
        it clears a stability test trivially. This gate catches prices that VISIBLY move (the honest
        failures, which the fire path's fresh-REST re-read catches anyway) and is blind to the
        dominant failure — a ticker quoting a price no book supports. That is the project's own
        selection effect (catchability anti-correlates with realness) aimed at the mechanism.

        What it still earns: (1) RATE LIMITING — it suppresses ~1019 REST reads on edges that would
        fail anyway, which is why it can't simply be deleted; (2) a weak, UNQUANTIFIED
        adverse-selection proxy (a surviving edge is plausibly likelier to outlast our ~100ms
        execution). It costs 300ms of every real edge's life — 75% of the edge→both-legs path.

        Not proof of profit either (see the settlement gate + Phase 1.5 backtest). The proposed
        replacement is a fresh-REST check AT DETECTION, firing immediately: same read volume, ~4x
        faster, authoritative filter. See docs/TODO.md S11."""
        now = time.time()
        current: set[str] = set()
        confirmed: list = []
        for opp in opps:
            key = f"{opp.poly_event_id}:{opp.kalshi_ticker}:{opp.kalshi_side}"
            current.add(key)
            seen = self._confirm_seen.get(key)
            if seen is None:
                self._confirm_seen[key] = (now, opp)
                # FIRST SIGHT — REST-verify before the 0.3s gate can kill it. This is the only
                # place an edge is observable pre-gate, and everything downstream only logs
                # post-gate survivors, so without this the gate cannot be judged without being
                # removed. DRY-only, off the hot path, never fires. Once per EPISODE (this branch
                # runs once per key) — a per-tick check would blow Poly's 1/s budget.
                # Check for the loop BEFORE building the coroutine: `create_task(coro())`
                # constructs coro() first, so if create_task raises the coroutine leaks
                # ("never awaited"). _confirm_persistent is a SYNC gate — scheduling from it
                # couples the decision path to the loop, so this is best-effort: no loop (unit /
                # sync context) → skip. Observability must NEVER break a gate.
                if config.DRY_RUN and _loop_running():
                    asyncio.create_task(self._probe_edge_at_detection(opp))
            else:
                first, _ = seen
                self._confirm_seen[key] = (first, opp)   # keep first-seen, refresh the opp
                if now - first >= config.KALSHI_CONFIRM_SECONDS:
                    confirmed.append(opp)
        # An edge that vanished (gone from `current`) before clearing the window never
        # persisted → log it rejected. One that DID clear the window already had its shot
        # downstream, so don't double-count it as a persistence rejection here.
        for k in [k for k in self._confirm_seen if k not in current]:
            first, last_opp = self._confirm_seen.pop(k)
            lived = now - first
            if lived < config.KALSHI_CONFIRM_SECONDS:
                self._log_reject(last_opp, "not_persistent",
                                 f"lived {lived:.2f}s < {config.KALSHI_CONFIRM_SECONDS}s")
        return confirmed

    async def _run_kalshi_execution(self) -> None:
        """Core Kalshi arb scan: execute opps, update edge lifecycle, log tightest."""
        if not self._kalshi_pairs:
            return
        if not self.kalshi_feed.subscriptions_ready:
            return
        if self._execution_lock.locked():
            return
        _poly_feed = self._poly_feed_adapter if self._poly_us else self.feed
        async with self._execution_lock:
            # Paused (stranded leg, kill switch, or daily-loss cap): don't trade, but
            # still record that fireable edges existed and were skipped — the ledger must
            # not go silent during a pause. (The edge-lifecycle CSV below also runs
            # unconditionally, so sub-min edges keep logging too.)
            if self.tracker.has_stranded() or self._trading_halted():
                for opp in find_kalshi_arbs(
                    self._kalshi_pairs, _poly_feed, self.kalshi_feed,
                    poly_balance=self._poly_buying_power,
                    kalshi_balance=self._kalshi_buying_power,
                ):
                    self._log_reject(opp, "paused", "stranded leg / kill-switch / loss-cap")
            else:
                dominated: list = []
                opps = find_kalshi_arbs(
                    self._kalshi_pairs, _poly_feed, self.kalshi_feed,
                    poly_balance=self._poly_buying_power,
                    kalshi_balance=self._kalshi_buying_power,
                    dominated_out=dominated,
                )
                # Exact-duplicate positions (same crossing via overlapping pairs) — the
                # only thing deduped now that every distinct direction fires. Rare.
                for d in dominated:
                    self._log_reject(d, "duplicate_position", f"edge={d.edge:.4f}")
                # Persistence confirm: only fire edges that have held for the confirm
                # window — sub-second goal-spike phantoms never clear it.
                for opp in self._confirm_persistent(opps):
                    if self.tracker.is_on_cooldown(self._direction_key(opp)):
                        self._log_reject(opp, "cooldown", f"direction {self._direction_key(opp)}")
                        continue
                    current_exposure = self.tracker.total_exposure()
                    if current_exposure + opp.total_cost > config.MAX_TOTAL_EXPOSURE:
                        self._log_reject(opp, "exposure_cap",
                                         f"${current_exposure:.0f}+${opp.total_cost:.0f} > ${config.MAX_TOTAL_EXPOSURE:.0f}")
                        log.warning(
                            f"Kalshi arb: exposure cap hit "
                            f"(${current_exposure:.0f}+${opp.total_cost:.0f} "
                            f"> ${config.MAX_TOTAL_EXPOSURE:.0f})"
                        )
                        continue
                    await self._execute_kalshi_arb(opp)

        # Edge-lifecycle CSV logging (START/SAMPLE/END + fat-spike capture) runs AFTER the execution
        # lock releases — pure terminal observability, extracted for legibility (verbatim move).
        self._log_edge_lifecycle(_poly_feed)

    def _log_edge_lifecycle(self, _poly_feed) -> None:
        """Track each profitable-edge lifecycle (START / SAMPLE-on-change / END+duration) into
        positive_edges.csv, plus the below-min-edge reject, the stdout EDGE line, and fat-spike
        capture. A SEPARATE find_kalshi_arbs(min_edge=1e-9) pass — logs ALL positive crossings, not
        just fireable ones. Pure observability: runs after the execution lock, mutates only its own
        lifecycle-tracking state (_active_edges / _edge_seq / _fat_spike_captured), which the
        execution path never reads. NOT on the fire path."""
        now = time.time()

        # Track profitable-edge lifecycle (start / sample every 5s / end + duration).
        current_keys: set[str] = set()
        _ms = int((now % 1) * 1000)
        _ts = time.strftime(f"%Y-%m-%dT%H:%M:%S.{_ms:03d}Z", time.gmtime(now))
        for opp in find_kalshi_arbs(
            self._kalshi_pairs, _poly_feed, self.kalshi_feed, min_edge=1e-9
        ):
            key = f"{opp.poly_event_id}:{opp.kalshi_ticker}:{opp.kalshi_side}"
            current_keys.add(key)
            # Columns: poly leg (incl. available depth), kalshi leg, then edge
            # metrics + sizing. (Team/side already live in their own columns, so
            # the game title stays clean.)
            _pd = _poly_feed.get_price(opp.poly_token)
            _poly_depth = getattr(_pd, "ask_depth", 0.0) if _pd else 0.0
            # Per-leg quote staleness at this instant, so each START/SAMPLE row is dateable without
            # joining edge_freshness. Blank (not 0) when a leg's feed has no quote yet.
            _p_age_s = f"{now - _pd.last_updated:.1f}" if _pd else ""
            _k_age = self.kalshi_feed.get_age(opp.kalshi_ticker)
            _k_age_s = f"{_k_age:.1f}" if _k_age is not None else ""
            _legs = [
                opp.event_title,
                opp.poly_team, f"{opp.poly_ask:.4f}", f"{_poly_depth:.0f}",
                opp.kalshi_team, opp.kalshi_side, opp.kalshi_ticker, f"{opp.kalshi_ask:.4f}",
            ]
            # Edge shown as a percentage (e.g. "1.85%"); dedup on the same string
            # so sub-0.01% jitter doesn't spawn rows.
            _edge_str = f"{opp.edge*100:.2f}%"
            if key not in self._active_edges:
                self._edge_seq += 1
                arb_id = self._edge_seq
                self._active_edges[key] = (now, now, opp.edge, opp.event_title, _edge_str, arb_id)
                self._pedge_csv.writerow(
                    [arb_id, _ts, "START"] + _legs +
                    [_edge_str, "", "", opp.shares, f"{opp.guaranteed_profit:.2f}",
                     _p_age_s, _k_age_s]
                )
                # Detected positive crossing but below the divergence-safety min-edge →
                # record the rejection (once per lifecycle START; positive crossings are
                # rare enough to log each, per the complete-rejection-ledger design).
                if opp.edge < config.KALSHI_ARB_MIN_EDGE:
                    self._log_reject(opp, "below_min_edge",
                                     f"edge={opp.edge:.4f} < min={config.KALSHI_ARB_MIN_EDGE}")
                # Surface meaningful profitable edges in stdout too (not just the CSV),
                # so you can watch them live without tailing positive_edges.csv. Gated
                # at the configured trade threshold so sub-threshold jitter doesn't spam.
                if opp.edge >= config.KALSHI_ARB_MIN_EDGE:
                    log.info(
                        f"💹 EDGE #{arb_id}: {opp.event_title} | "
                        f"poly {opp.poly_team}@{opp.poly_ask:.3f} + "
                        f"kalshi {opp.kalshi_team} {opp.kalshi_side}@{opp.kalshi_ask:.3f} | "
                        f"edge {_edge_str} shares={opp.shares} profit=${opp.guaranteed_profit:.2f}"
                    )
            else:
                first_ts, _, peak, game, last_edge_str, arb_id = self._active_edges[key]
                new_peak = max(peak, opp.edge)
                # Always refresh last-seen time + peak so the END row's duration is
                # accurate, but only write a SAMPLE row when the edge actually
                # changes — a long-lived arb at a constant edge would otherwise
                # spam identical rows with no added information.
                if _edge_str != last_edge_str:
                    self._active_edges[key] = (first_ts, now, new_peak, game, _edge_str, arb_id)
                    self._pedge_csv.writerow(
                        [arb_id, _ts, "SAMPLE"] + _legs +
                        [_edge_str, f"{new_peak*100:.2f}%", f"{now - first_ts:.1f}s", "", "",
                         _p_age_s, _k_age_s]
                    )
                else:
                    self._active_edges[key] = (first_ts, now, new_peak, game, last_edge_str, arb_id)

            # ── Fat-spike diagnostic capture (>5% edges only; pure observability) ────────────
            # Single insertion after the if/else merge: arb_id is set by BOTH branches above and
            # _poly_depth at the loop top (line ~1072), so all locals are in scope regardless of
            # branch. The gate is a float compare + set lookup — sub-5% edges add nothing and the
            # normal path is unchanged; no REST read happens here (it's inside the async task).
            # FREEZE the detection-instant WS snapshot by value before scheduling so the WS baseline
            # can't move under the WS-vs-REST comparison.
            if opp.edge > _FAT_SPIKE_EDGE_THRESHOLD and arb_id not in self._fat_spike_captured:
                self._fat_spike_captured.add(arb_id)
                asyncio.create_task(self._capture_fat_spike(
                    arb_id, opp,
                    _FatSpikeWS(
                        poly_ask=opp.poly_ask_raw,
                        poly_depth=_poly_depth,
                        kalshi_ask=opp.kalshi_ask,
                        kalshi_depth=self.kalshi_feed.get_depth(opp.kalshi_ticker, opp.kalshi_side),
                    ),
                    time.time(),
                ))

        # Detect edges that just ended and write a final END row with total duration.
        for key in list(self._active_edges.keys()):
            if key not in current_keys:
                first_ts, last_ts, peak, game, _, arb_id = self._active_edges.pop(key)
                self._fat_spike_captured.discard(arb_id)   # release the dedup id on lifecycle end
                self._pedge_csv.writerow(
                    [arb_id, _ts, "END", game,
                     "", "", "", "", "", "", "",       # poly (team/ask/depth) + kalshi legs blank on END
                     "", f"{peak*100:.2f}%", f"{last_ts - first_ts:.1f}s", "", "",
                     "", ""]                            # poly_age_s/kalshi_age_s: legs not re-read on END
                )

    async def _kalshi_arb_loop(self) -> None:
        """1-second fallback loop for Kalshi arb detection (event-driven via _trigger_kalshi_detect)."""
        if not config.KALSHI_API_KEY:
            return
        _last_tightest_log: float = 0.0
        while True:
            await asyncio.sleep(0.25)
            if not self._kalshi_pairs:
                # No matched cross-arb pairs (e.g. no overlapping games right now — Poly lists only
                # futures/champions, or the day's games haven't opened). Heartbeat at the same 10s
                # cadence as the tightest line so the arb side stays VISIBLE and distinguishable
                # from a dead logger; resumes the full tightest line as soon as pairs match.
                _now = time.time()
                if _now - _last_tightest_log >= 10.0:
                    _last_tightest_log = _now
                    tightest_log.info(
                        f"Kalshi TIGHTEST | no matched cross-arb pairs "
                        f"({len(getattr(self, 'active_pairs', []) or [])} Poly games) — arb side idle"
                    )
                continue
            if not self.kalshi_feed.subscriptions_ready:
                continue

            # Always run: _run_kalshi_execution self-skips trading while stranded
            # but still logs the edge lifecycle, so the pause doesn't blind us.
            # Guarded: this loop is the SAFETY NET behind the WS-driven path, so one bad
            # iteration must never kill it. Unguarded, any raise from the exec path (a venue
            # error, a parse bug) ended the loop permanently and silently — detection just stopped.
            try:
                await self._run_kalshi_execution()
            except Exception as e:
                log.critical(f"_run_kalshi_execution raised (loop survives): {e!r}")

            now = time.time()

            # Log tightest pair every 10s regardless of whether an arb was found.
            # Also write to the near-miss CSV for any edge within 5 cents of breakeven.
            if now - _last_tightest_log >= 10.0:
                _last_tightest_log = now
                _poly_feed_t = self._poly_feed_adapter if self._poly_us else self.feed
                tightest = find_kalshi_tightest(self._kalshi_pairs, _poly_feed_t, self.kalshi_feed)
                if tightest:
                    gap = -tightest.edge  # gap > 0 = no arb; gap < 0 = arb
                    _cg = f"\033[32m{gap:+.4f}\033[0m" if gap < 0 else f"{gap:+.4f}"
                    _k_age = self.kalshi_feed.get_age(tightest.kalshi_ticker)
                    _k_age_str = f"{_k_age:.0f}s" if _k_age is not None else "?"
                    _p_data = _poly_feed_t.get_price(tightest.poly_token)
                    _p_age = (now - _p_data.last_updated) if _p_data else None
                    _p_age_str = f"{_p_age:.0f}s" if _p_age is not None else "?"
                    _dist = f"\033[32mVIABLE\033[0m" if tightest.edge >= 0.005 else f"need {gap:.4f} more to arb"
                    _combined = tightest.poly_ask + tightest.kalshi_ask + _kalshi_taker_fee(tightest.kalshi_ask)
                    tightest_log.info(
                        f"Kalshi TIGHTEST | '{tightest.event_title}' "
                        f"poly={tightest.poly_team}@{tightest.poly_ask:.3f}(age={_p_age_str}) "
                        f"kalshi={tightest.kalshi_team}[{tightest.kalshi_side}]@{tightest.kalshi_ask:.3f}(age={_k_age_str}) "
                        f"combined={_combined:.4f} "
                        f"gap={_cg} [{_dist}]  ({len(self._kalshi_pairs)} matched)"
                    )
                    if tightest.edge >= 0.005:
                        _poly_depth = getattr(_p_data, "ask_depth", 0.0) if _p_data else 0.0
                        # AUTHORITATIVE REST fillable-at-ask (replaces the prior "n/a" filler with a
                        # real, dated number) — shared self-dating sentinel, see _kalshi_rest_depth_str.
                        _k_depth = self._kalshi_rest_depth_str(
                            tightest.kalshi_ticker, tightest.kalshi_side, tightest.kalshi_ask, now)
                        self._edge_csv.writerow([
                            time.strftime("%Y-%m-%dT%H:%M:%S.{:03d}Z".format(int((now % 1) * 1000)), time.gmtime(now)),
                            tightest.event_title,
                            tightest.poly_team,
                            tightest.poly_token,
                            f"{tightest.poly_ask_raw:.4f}",
                            f"{tightest.poly_ask:.4f}",
                            f"{_p_age:.1f}" if _p_age is not None else "",
                            f"{_poly_depth:.0f}",
                            tightest.kalshi_ticker,
                            tightest.kalshi_side,
                            tightest.kalshi_team,
                            f"{tightest.kalshi_ask:.4f}",
                            f"{_k_age:.1f}" if _k_age is not None else "",
                            _k_depth,
                            f"{tightest.edge:.6f}",
                            f"{_combined:.6f}",
                        ])
                else:
                    # Pairs are matched but no leg has a live quote yet (feed warming/quiet) —
                    # keep the arb line alive rather than going silent.
                    tightest_log.info(
                        f"Kalshi TIGHTEST | {len(self._kalshi_pairs)} matched pairs, no live quote yet"
                    )
