"""Shared runner-level helpers: file loggers, throttle constants, the Poly US
feed adapter, and the FOK slippage buffer. Imported by the BotRunner mixins."""
from __future__ import annotations

import time

from bot.core import config
from bot.core.logger import get_file_logger
from bot.core.matcher import parse_iso
from bot.poly_us.feed import PolyUSOrderBookCache
from bot.poly_us.sides import parse_token

# Logging convention (so new logging picks the right pattern):
#   • These dedicated file loggers (tightest/fills/orderbook) — single-line, free-form execution
#     or diagnostic records written directly via `<name>_log.info(...)`.
#   • The `_log_*` methods in kalshi_arb.py (`_log_would_fire`, `_log_fill_success`, `_log_reject`,
#     `_log_edge_lifecycle`, …) — STRUCTURED CSV rows (registered header + blank-not-zero formatting
#     via `fmt_or_blank`), one method per schema. Use a `_log_*` method for anything columnar.
# High-frequency TIGHTEST diagnostics go to a file, not the console.
tightest_log = get_file_logger("tightest", "tightest.log")
# Every execution attempt's leg-fill outcome + raw venue responses.
fills_log = get_file_logger("fills", "fills.log")
# Periodic Kalshi orderbook health (coverage, seq, gaps/resnaps, REST divergence).
orderbook_log = get_file_logger("orderbook", "orderbook.log")
# Min seconds between "missed" (no-fill) alerts for the same match — true misses
# move no money but can re-fire often, so throttle the heads-up.
_MISS_ALERT_THROTTLE_S = 300
# Min seconds between diagnostic Kalshi orderbook snapshots per ticker. Low so
# we get good phantom-edge frequency data; still bounds REST on a re-firing book.
_BOOK_LOG_THROTTLE_S = 5
# Min seconds between rejected-edge CSV rows for the same (event:ticker:side:reason).
# Low so the reject ledger captures edge churn at decent resolution without per-tick flood.
_REJECT_LOG_THROTTLE_S = 15


class _PolyUSPriceData:
    """Thin shim so PolyUSOrderBookCache prices look like TokenPriceData to callers."""
    __slots__ = ("best_ask", "ask_depth", "best_bid", "last_updated")

    def __init__(self, best_ask: float, ask_depth: float, last_updated: float,
                 best_bid: float | None = None) -> None:
        self.best_ask = best_ask
        self.ask_depth = ask_depth
        self.best_bid = best_bid   # price an unwind sells into (flatten cost); None if no bid
        self.last_updated = last_updated


class _PolyUSFeedAdapter:
    """Wraps PolyUSOrderBookCache to expose the get_price() interface used by
    find_kalshi_arbs / find_macro_arbs / find_kalshi_tightest."""

    def __init__(self, us_feed: PolyUSOrderBookCache) -> None:
        self._us_feed = us_feed

    def get_price(self, slug: str):
        ask = self._us_feed.get_best_ask(slug)
        # Treat a missing or >= 1.0 ask as "no real price" (matches the global
        # feed convention: 1.0 is the empty-book sentinel, never a tradeable ask).
        # This prevents primed-but-unquoted slugs from producing phantom arbs.
        if ask is None or ask >= 1.0:
            return None
        age = self._us_feed.get_age(slug)
        ts = (time.time() - age) if age is not None else time.time()
        depth = self._us_feed.get_depth(slug) or 0.0
        bid = self._us_feed.get_best_bid(slug)
        return _PolyUSPriceData(best_ask=ask, ask_depth=depth, last_updated=ts, best_bid=bid)

    # Passthrough for subscriptions_ready so callers that check it still work.
    @property
    def subscriptions_ready(self) -> bool:
        return self._us_feed.subscriptions_ready


def fmt_or_blank(x, fmt: str) -> str:
    """Format a number, or "" when it's None/not-a-number — the blank-not-zero CSV discipline:
    a real 0 is "measured zero", a blank is "no data". Single source for the writers' inline
    formatter (was copy-pasted as _g/_f in three kalshi_arb log methods). NEVER emit "0" for None."""
    return fmt.format(x) if isinstance(x, (int, float)) else ""


def _fok_buffer(edge: float) -> float:
    """Kalshi's MINIMUM-PROFIT FLOOR, scaled to the edge. Returns max(KALSHI_FOK_BUFFER,
    edge * KALSHI_FOK_EDGE_FRACTION / 2), capped at 0.45*edge.

    ⚠️ READ THE SIGN BEFORE TUNING: this is NOT a slippage allowance, and raising it does NOT buy
    room to fill through a fast move. Since S4 (2026-07-16) `buf` has exactly ONE reader — the
    Kalshi limit — and it TIGHTENS it:

        kalshi_limit = _kalshi_breakeven_ask(poly_eff, buf) = 1 - poly_eff - buf

    i.e. the limit sits at BREAKEVEN-minus-buf against the detected Poly cost, so a bigger buf
    demands MORE profit and fills LESS often. That is the intended job: buf is the slice of the
    edge we refuse to give up. Kalshi's own allowance is the rest, `edge - buf`.
    (Verified at edge=0.0304: buf=0.0053; kalshi_limit 0.71420 -> 0.70860 as buf rises.)

    Two dead models this docstring used to carry, both corrected on measurement:
      • "combined slippage is 2*buf, evenly split" — never true. It was Poly=buf, Kalshi=edge-buf,
        with nearly all the room on one leg. [Corrected 2026-07-15.]
      • "fat edges get a big buffer to fill through fast moves" — backwards, per the sign above.

    The Poly leg no longer reads this at all: `poly_limit` is the raw ask, flat. Its cushion was
    measured to rescue 0 of 14 poly_moved states and was dropped — the reasoning, the numbers, and
    the ceil-to-tick alternative are in `kalshi_arb.fire_limits`, which is where the asymmetry is
    now visible in one place. (That cushion was also silently tick-dependent: it floored away to
    nothing on a 0.01-tick market and survived as a full 1-tick cushion on a 0.005-tick one, and
    the live slate is a ~50/50 mix of both. The tick is a VENUE fact — read it from
    `MarketPair.poly_tick` or the venue-reference skill, never restate it here; the last docstring
    that did was wrong in every part for months.)

    The name is legacy: both legs are IOC now (Poly rewrites FOK -> IOC; Kalshi we send IOC
    deliberately), so nothing here is FOK-specific.
    """
    buf = max(config.KALSHI_FOK_BUFFER, edge * config.KALSHI_FOK_EDGE_FRACTION / 2.0)
    return min(buf, edge * 0.45)


def schema_drift_alerts(
    series_universe: list[str],
    kalshi_counts: dict[str, int],
    poly_event_counts: dict[str, int],
    poly_pair_counts: dict[str, int],
    matched_counts: dict[str, int],
    broken_cycles: dict[str, int],
    alerted: set[str],
    debounce: int = 2,
    min_poly_events: int = 2,
) -> tuple[list[tuple[str, int, int, int]], list[str]]:
    """Schema-drift detector (pure). A sport is "broken" when BOTH venues clearly have a live slate
    — Kalshi lists markets AND Poly fetched ≥`min_poly_events` raw game events — yet 0 matched
    cross-pairs result. That is the silent venue-schema-rename signal (2026-06-23: Poly renamed MLB's
    sportsMarketType → 0 pairs → 0 matches, blind ~5.5h).

    Why gate on Poly raw EVENTS, not pairs (the 2026-06-24 false-positive fix): a parse/rename break
    has events fetched but 0 pairs built (events>0, pairs=0); a normal BETWEEN-SLATES lull has no
    games on Poly at all (events=0) while Kalshi keeps scheduled markets listed all day — so the old
    "Kalshi has markets" oracle cried wolf every night. Pairs can't tell these apart (both 0). Raw
    events can: 0 events = no games (silent); a full slate of unpaired events = a real break (fire).
    The `min_poly_events` floor silences a SINGLE stranded unpaired market (1 event: e.g. an all-day
    WC market with no Kalshi counterpart). Floor=2 (not 3): catches even a 2-game slate break, with
    the trade-off that 2 *simultaneously* stranded markets would false-fire — rarer than a real
    2-game break is worth catching.

    Stateful across cycles via the caller-owned `broken_cycles` + `alerted` (MUTATED in place):
    `debounce` consecutive broken cycles before alerting (kills the market-open race), warn once per
    episode, re-arm on recovery.

    Returns (to_alert, recovered): `to_alert` = (series, kalshi_n, poly_events, poly_pairs) tuples that
    newly crossed into the alert state; `recovered` = series that healed since last alerted.
    """
    to_alert: list[tuple[str, int, int, int]] = []
    recovered: list[str] = []
    for s in series_universe:
        kn = kalshi_counts.get(s, 0)
        mn = matched_counts.get(s, 0)
        pe = poly_event_counts.get(s, 0)
        # broken = both venues have a real slate (Kalshi markets + a slate of Poly game events) but
        # 0 matched cross-pairs. Gating on pe ≥ min_poly_events is the false-positive fix.
        if mn == 0 and kn > 0 and pe >= min_poly_events:
            broken_cycles[s] = broken_cycles.get(s, 0) + 1
            if broken_cycles[s] >= debounce and s not in alerted:
                alerted.add(s)
                to_alert.append((s, kn, pe, poly_pair_counts.get(s, 0)))
        else:
            broken_cycles[s] = 0
            if s in alerted:
                alerted.discard(s)
                recovered.append(s)
    return to_alert, recovered


_WINDOW_PRE_BUFFER_S = 0.0           # arm at startTime (pre-game quiet stays correctly suppressed)
_WINDOW_POST_BUFFER_S = 4 * 3600.0   # err LONG past endTime — cover overruns; benign post-game cry-wolf
_WINDOW_MISSING_END_S = 8 * 3600.0   # generous window when endTime is absent (start + this)

# Fire-gate peer-window fallback when endTime is absent. DELIBERATELY TIGHTER than the watchdog's
# err-long _WINDOW_*_BUFFER_S above: the origin-freeze fire gate must NOT count a finished/quiet game
# as a freeze peer (a post-game market is legitimately stale, not freeze evidence), so its window ends
# AT the real endTime (no post-buffer) and a missing-endTime game uses this modest game-length cap, not
# the watchdog's 8h. Errs SHORT (fail toward firing: drop a peer rather than over-confirm a freeze).
_FIRE_PEER_MAX_GAME_S = 4 * 3600.0


def in_window_slugs(pairs, now: float) -> set[str]:
    """Bare slugs of pairs whose REAL game window [start, end] contains `now` — the PEER SCOPE for the
    origin-freeze fire gate (PolyUSOrderBookCache.count_stale_books). A stale peer corroborates a freeze
    ONLY if it's a market that SHOULD be updating right now (a live game); a pre-game (now<start) or
    finished (now>end) market is legitimately quiet, so its staleness is NOT freeze evidence and must be
    EXCLUDED — else upcoming/finished games in the discovery slate falsely confirm a freeze and the gate
    rejects a real illiquid-but-live edge (the §5 tail it exists to preserve).

    FIRE-GATE fail-direction — the OPPOSITE of the watchdog's build_game_windows/_books_should_move (which
    err toward ARMED/watching on unreadable timing). Here, uncertainty EXCLUDES a peer (fail toward firing,
    never suppress a real edge on uncertain data): a pair with an unreadable start_date is dropped; the
    window has NO pre/post buffer (tight to the game, so a finished game leaves the peer set at endTime,
    not 4h later); a missing endTime uses the modest _FIRE_PEER_MAX_GAME_S cap, not the watchdog's 8h.
    Returns BARE slugs (parse_token), matching how _transact_times / _prices are keyed."""
    out: set[str] = set()
    for pair in pairs:
        start_dt = parse_iso(getattr(pair, "start_date", None))
        if start_dt is None:
            continue                                   # unreadable start → excluded (fail toward firing)
        start = start_dt.timestamp()
        end_dt = parse_iso(getattr(pair, "end_date", None))
        end = end_dt.timestamp() if end_dt is not None else start + _FIRE_PEER_MAX_GAME_S
        if start <= now <= end:
            for slug in (getattr(pair, "token_yes_a", None), getattr(pair, "token_yes_b", None)):
                if slug:
                    out.add(parse_token(slug)[0])
    return out


def build_game_windows(pairs) -> tuple[list[tuple[float, float]], bool]:
    """Active-game windows for the freshness watchdog gate. Returns (windows, unknown).

    Each pair with a PARSEABLE start_date yields an epoch-seconds window:
        (start - _WINDOW_PRE_BUFFER_S,  (end_date or start + _WINDOW_MISSING_END_S) + _WINDOW_POST_BUFFER_S)
    The window-end is err-LONG by construction (the venue's own endTime padded, or a generous fallback) so
    a game running long keeps the watchdog ARMED — never blind mid-game.

    `unknown` is the OR across ALL pairs of "this pair's start_date is missing/unparseable". If ANY game's
    timing can't be read, the caller MUST treat the watchdog as ARMED (err toward watching — an unreadable
    game might be live). So: empty pair list → ([], False) (no games → watchdog may rest); ALL-unparseable
    list → ([], True) (armed, NOT suppressed); mixed → windows for the readable ones + unknown=True."""
    windows: list[tuple[float, float]] = []
    unknown = False
    for pair in pairs:
        start_dt = parse_iso(getattr(pair, "start_date", None))
        if start_dt is None:
            unknown = True                       # missing/unparseable start → can't bound this game → arm
            continue
        start = start_dt.timestamp()
        end_dt = parse_iso(getattr(pair, "end_date", None))
        end = end_dt.timestamp() if end_dt is not None else start + _WINDOW_MISSING_END_S
        windows.append((start - _WINDOW_PRE_BUFFER_S, end + _WINDOW_POST_BUFFER_S))
    return windows, unknown
