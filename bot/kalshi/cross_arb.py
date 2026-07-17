"""
bot/kalshi/cross_arb.py
───────────────────────
Cross-platform arbitrage detection: Kalshi ↔ Polymarket US.

Both venues are binary, $1-payout markets (NOT decimal-odds sportsbooks — that was the legacy
stack). A hedge buys the two complementary outcomes of one game, one leg per venue, so exactly
one leg pays $1 regardless of who wins; the edge is how far the combined entry cost falls below $1.

The math
--------
Two opposing legs on the same game:
  Leg 1 (Polymarket US): buy "poly_team wins" at ask p_poly. Poly charges a parabolic taker fee,
    so the effective per-share cost is
      poly_effective = p_poly + Θ·p_poly·(1 − p_poly)     (Θ from feeCoefficient; see _effective_share_cost)
  Leg 2 (Kalshi): buy the COMPLEMENTARY side at ask p_kalshi (yes/no chosen so the two legs cover
    opposite outcomes), effective cost
      kalshi_effective = p_kalshi + ceil_centicent(0.07·p_kalshi·(1 − p_kalshi))  (see _kalshi_taker_fee)
      ⚠️ CENTICENT ($0.0001), not cent — ceiling to the cent was a real bug (fixed 2026-07-14) that
      overstated the fee and UNDERSTATED every edge; see _kalshi_taker_fee's docstring.

Arb condition (one leg always pays $1, so the hedge profits iff combined cost < $1):
  poly_effective + kalshi_effective < 1.0
Edge = 1.0 − (poly_effective + kalshi_effective)

Sizing (shares is the independent variable; both legs take the same share count since each pays
$1 on its outcome):
  position_usd = MAX_POSITION_USD
  shares       = floor(position_usd / max(poly_effective, kalshi_effective))   (capped by book depth + per-leg balance)
  total_cost   = shares × (poly_effective + kalshi_effective)

Settlement equivalence (the load-bearing safety boundary) is enforced DOWNSTREAM by the matcher's
allowlist, not here — see bot/kalshi/matcher.py:is_settlement_equivalent.
"""
from __future__ import annotations

import math
import time
import requests as _requests
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from bot.core import config
from bot.core.logger import get_logger
from bot.core.matcher import parse_iso, _names_match

if TYPE_CHECKING:
    from bot.kalshi.macro_pairs import MacroPair

log = get_logger(__name__)

def _effective_share_cost(p_poly: float, fee_rate: float = 0.0) -> float:
    """
    Effective cost per share purchased on Polymarket (including taker fee).

    Payout on resolution is always $1.00 per share; the taker fee raises the entry cost:

        fee (USDC, per ORDER) = round(Θ · C · p · (1-p), 2)      ← rounded to the CENT on the TOTAL
        fee_per_share         = Θ · p · (1-p)
        effective_cost        = p + Θ · p · (1-p)

    [VERIFIED 2026-07-15 against 9 REAL exchange commissions (commissionNotionalTotalCollected,
    read back from /v1/order/{id} + portfolio/activities across p=0.008…0.67). All 9 match the
    formula EXACTLY at the Θ in force when they traded. Pinned: tests/test_poly_fee_model.py.
    Previously this docstring cited "Polymarket's published schedule" with no first-party check —
    the same docs-say-so provenance that produced the Kalshi cent-vs-centicent bug.]

    Θ comes from the market's own `feeCoefficient` (scanner._event_fee_rate), NOT a constant —
    Poly RAISED it 0.05 → 0.06 between 2026-06-17 and 2026-07-15 and the live path self-corrected
    because it reads the venue per-market. Surcharge is symmetric around p=0.5 and peaks there:
    at Θ=0.06, p=0.5 adds 0.015/share ($1.50 per 100 shares).

    ⚠️ NOTHING IS FEE-FREE. A previous version claimed "NBA/NHL (fee_rate=0): effective_cost =
    p_poly" — FALSE: all 29 live pairs, including all 17 NBA, report feeCoefficient=0.06. The
    fee_rate<=0 branch below is a defensive no-fee path, NOT a description of any live sport.

    NOT MODELLED: the round-to-cent on the order total (±$0.005/order — ±0.06¢/share at C=8,
    ±0.005¢ at C=100). It is a symmetric ROUND, so it is unbiased noise, not a systematic
    understatement — immaterial against the 2% floor at our sizes. Revisit if size drops to ~1.
    """
    if fee_rate <= 0:
        return p_poly
    return p_poly + fee_rate * p_poly * (1 - p_poly)


def resize_opportunity(opp, new_shares: int) -> None:
    """Size an opportunity DOWN, in place, to `new_shares`. Never grows it; no-op at <=0.

    Sizing happens at DETECTION off the WS cache (_size_kalshi_opportunity), which caps on Poly
    depth + balances only — it never knew the Kalshi book, on the false premise that "Kalshi fills
    any size at its quote". The authoritative Kalshi depth only arrives at FIRE time (fresh REST),
    so the fire path re-sizes here rather than rejecting outright: under IOC we can fill whatever
    the book holds, and rejecting throws away the 17% of events where it holds SOME depth.

    total_cost and guaranteed_profit are LINEAR in shares, so this is exact — but they MUST move
    WITH shares: _record_hedge computes `ratio = qty/opp.shares` then `guaranteed_profit * ratio`,
    so changing shares alone would book the ORIGINAL profit on a smaller order. Never grows: only
    real depth may size us, and growing would invent size no book supports.
    """
    n = int(new_shares)
    if opp.shares <= 0 or n <= 0 or n >= opp.shares:
        return
    per_share_cost = opp.total_cost / opp.shares
    opp.shares = n
    opp.total_cost = n * per_share_cost
    opp.guaranteed_profit = n * 1.0 - opp.total_cost

# Kalshi sports series that use a single two-outcome (no-tie) market structure,
# vs soccer's 3-way (win/draw/loss). Used by find_kalshi_arbs direction logic.
# KXWNBAGAME is two-outcome like NBA (no draw) → cross-pair directions are valid.
_TWO_OUTCOME_SERIES: frozenset[str] = frozenset({"KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXWNBAGAME"})


@dataclass
class KalshiArbOpportunity:
    event_title: str
    poly_event_id: str       # Polymarket event_id (for cooldown tracking)
    poly_token: str          # Token ID to buy on Polymarket
    poly_team: str           # Human label (for logging)
    poly_ask_raw: float      # Raw Polymarket ask price (for order placement)
    poly_ask: float          # Effective per-share cost including Poly fee (for arb math)
    kalshi_ticker: str       # Kalshi market ticker
    kalshi_side: str         # "yes" or "no" — which side to buy
    kalshi_team: str         # Human label (for logging)
    kalshi_ask: float        # Raw Kalshi ask price (before fee)
    edge: float              # 1.0 - poly_effective - kalshi_effective
    shares: int              # Contracts on each leg
    total_cost: float        # shares × (poly_effective + kalshi_effective)
    guaranteed_profit: float # shares × 1.0 - total_cost
    kalshi_tick: float = 0.01  # the market's price tick (from price_ranges[].step; default 1¢)
    poly_tick: float | None = None  # the POLY market's tick (MarketPair.poly_tick). Logging-only,
    # and unlike kalshi_tick it has NO default: kalshi_tick needs one because kalshi_tick_floor
    # divides by it on the fire path, whereas nothing computes with this — so None stays honest
    # about an unread tick instead of asserting the majority value.
    kickoff: str | None = None  # game start (ISO, = Poly start_date/gameStartTime); for log game-phase
    detected_ts: float | None = None  # time.time() at THIS loop's detection (stamped in
    # find_kalshi_arbs) — for fill_success.csv's detection→re-read fill-window latency. Observability
    # only; never read by the fire path. A fresh opp is built each loop, so this is this-loop
    # detection (the 0.5s persistence wait is excluded — see _confirm_persistent).


def kalshi_tick_floor(price: float, tick: float = 0.01) -> float:
    """Round a price DOWN to the nearest Kalshi price tick (`tick`, default 1¢).

    Kalshi rejects orders priced off a whole-tick boundary (HTTP 400). We round the
    price we'd pay DOWN — never up — so a tick adjustment can only make the trade
    cheaper (more profitable), never push it past breakeven. The round() before
    floor() absorbs float noise (e.g. 0.29/0.01 == 28.999999996 → 29, not 28).
    `tick` is read live from the market (`price_ranges[].step`); the 0.01 default is the
    fallback — every binary market is `linear_cent` today, so this is defensive, not a
    behavior change. A 0/None tick would divide-by-zero, so callers pass a positive tick
    (the scanner's _parse_price_tick guarantees ≥ 0.01)."""
    return math.floor(round(price / tick, 6)) * tick


def _kalshi_breakeven_ask(poly_effective: float, buffer: float) -> float:
    """Max Kalshi ask price at which the arb remains profitable.

    Solves: ask + _kalshi_taker_fee(ask) = 1.0 - poly_effective - buffer
    iteratively (converges in 4 steps; fee function is smooth and nearly linear).
    """
    target = 1.0 - poly_effective - buffer
    ask = target - 0.02  # seed: midpoint fee is ~$0.02
    for _ in range(4):
        ask = target - _kalshi_taker_fee(max(0.01, min(0.99, ask)))
    return round(max(0.01, min(0.99, ask)), 4)


def _kalshi_taker_fee(price: float, count: int = 1) -> float:
    """Per-contract Kalshi taker fee, ceiled to the CENTICENT ($0.0001) on the ORDER TOTAL.

    Kalshi's published formula (docs/kalshi-fee-schedule.pdf, effective 2026-07-07), verbatim:
        fees = round up(M x 0.07 x C x P x (1-P))
        "round up = rounds up such that the fee + positionCost is rounded to a centicent"
    M (taker multiplier) defaults to 1, and is explicitly 1 for every series we trade
    (KXMLBGAME / KXWCGAME / KXWNBAGAME are listed at taker M=1; KXNBAGAME / KXNHLGAME are not
    listed at all, so they take the default 1) — so M is omitted here. Ceiling applies to the
    order TOTAL, not per contract (measured: 3 @ 0.45 → $0.0520, not $0.0522).

    ⚠️ CENTICENT (1e-4), **NOT** cent. The schedule's convenience table ("$0.30 → $0.02 for 1
    contract", pp.4-5) is DISPLAY-rounded up to the cent and is NOT the formula — the same
    table's 100-contract column shows the truth ($30.00 → $1.47 = 0.07·100·0.3·0.7 exactly).
    Implementing that table was the original bug (fixed 2026-07-14): it overstated the fee by
    up to ~0.9c/contract, which UNDERSTATED every edge — ~10% of `below_min_edge` rejects
    (327/3234) actually cleared the 2% net floor. Read the formula, never the table.

    VERIFIED against real fills 2026-07-14 (pinned in tests/test_kalshi_fee_model.py):
      prod  1 @ 0.4270 → $0.017200   (sub-cent price; centicent ceil of 0.017127)
      demo  3 @ 0.45   → $0.052000   (excludes per-contract ceiling, which gives 0.0522)
      demo  1 @ 0.30   → $0.014700   (exact centicent; excludes our old $0.02)
    """
    # round() BEFORE ceil() absorbs float noise — same guard as kalshi_tick_floor. Critical at
    # centicent precision: 0.07*0.5*0.5*10000 == 175.00000000000003 in float, so a bare ceil()
    # yields 176 → $0.0176 instead of the true $0.0175 (the schedule's own 100-contract column
    # confirms $50.00 → $1.75). The old cent-ceiling hid this; 100x finer precision exposes it.
    total = math.ceil(round(0.07 * count * price * (1 - price) * 10000, 6)) / 10000
    return total / count


def check_kalshi_arb(
    poly_ask: float,
    kalshi_ask: float,
    poly_fee_rate: float = 0.0,
) -> float:
    """Return the post-fee arb edge.  Positive = arb exists.  Negative = no arb but shows distance to breakeven.

    Both legs pay $1.00 on the winning outcome, so:
      edge = 1.0 - poly_effective - kalshi_effective
    """
    if not (0.0 < poly_ask < 1.0 and 0.0 < kalshi_ask < 1.0):
        return -999.0
    poly_effective   = _effective_share_cost(poly_ask, poly_fee_rate)
    kalshi_effective = kalshi_ask + _kalshi_taker_fee(kalshi_ask)
    return 1.0 - poly_effective - kalshi_effective


def _size_kalshi_opportunity(
    pair: CrossPlatformPair,
    poly_side: str,      # "a" or "b"
    kalshi_side: str,    # "yes" or "no"
    poly_ask_raw: float,
    kalshi_ask: float,
    poly_fee_rate: float,
    edge: float,
    poly_ask_depth: float = 0.0,                 # shares available at the Poly best ask (0 = unknown)
    poly_balance: float | None = None,           # available Poly buying power (None/0 = no cap)
    kalshi_balance: float | None = None,         # available Kalshi buying power (None/0 = no cap)
    kalshi_ticker_override: str | None = None,  # cross-pair: use a different Kalshi market
    kalshi_team_override: str | None = None,    # cross-pair: use a different team label
) -> KalshiArbOpportunity:
    poly_effective   = _effective_share_cost(poly_ask_raw, poly_fee_rate)
    kalshi_effective = kalshi_ask + _kalshi_taker_fee(kalshi_ask)

    # Size = the per-trade budget over the DEARER leg. No edge multiplier: CROSS_ARB_SIZE_TIERS
    # was deleted 2026-07-15 (its 0.005/0.01 breakpoints sat below the 0.02 fire floor, so it was a
    # constant 1.0 on every live path). Do NOT re-add edge-scaled sizing without re-tuning ABOVE
    # the floor — and never size UP on fat edges: cross-venue, a fat edge means the two venues
    # DISAGREE, i.e. one is stale, which is peak strand risk. See docs/TODO.md S1.
    position_usd = config.MAX_POSITION_USD
    shares       = max(1, math.floor(position_usd / max(poly_effective, kalshi_effective)))
    # Cap at available Polymarket depth so the FOK fills instead of being killed.
    # Kalshi uses a market-maker model and fills any size at its quote, so only
    # the Polymarket leg's book depth constrains us. Fall back to the budget size
    # when depth is unknown (0).
    if isinstance(poly_ask_depth, (int, float)) and poly_ask_depth > 0:
        shares = max(1, min(shares, math.floor(poly_ask_depth)))
    # Cap at affordable balance per leg (95% buffer for fees/price drift) so an
    # oversized leg can't fail for insufficient funds and strand the other leg.
    if isinstance(poly_balance, (int, float)) and poly_balance > 0:
        shares = max(1, min(shares, math.floor(poly_balance * 0.95 / poly_effective)))
    if isinstance(kalshi_balance, (int, float)) and kalshi_balance > 0:
        shares = max(1, min(shares, math.floor(kalshi_balance * 0.95 / kalshi_effective)))

    total_cost        = shares * (poly_effective + kalshi_effective)
    guaranteed_profit = shares * 1.0 - total_cost

    if poly_side == "a":
        poly_token  = pair.poly_pair.token_yes_a
        poly_team   = pair.poly_team_a
        kalshi_team = kalshi_team_override or (
            pair.kalshi_no_team if kalshi_side == "no" else pair.kalshi_yes_team
        )
    else:
        poly_token  = pair.poly_pair.token_yes_b
        poly_team   = pair.poly_team_b
        kalshi_team = kalshi_team_override or (
            pair.kalshi_yes_team if kalshi_side == "yes" else pair.kalshi_no_team
        )

    return KalshiArbOpportunity(
        event_title=pair.poly_pair.event_title,
        poly_event_id=pair.poly_pair.event_id,
        poly_token=poly_token,
        poly_team=poly_team,
        poly_ask_raw=poly_ask_raw,
        poly_ask=poly_effective,
        kalshi_ticker=kalshi_ticker_override or pair.kalshi_market.ticker,
        kalshi_side=kalshi_side,
        kalshi_team=kalshi_team,
        kalshi_ask=kalshi_ask,
        edge=edge,
        shares=shares,
        total_cost=total_cost,
        guaranteed_profit=guaranteed_profit,
        # Price tick read live from the matched market (all linear_cent today → 0.01);
        # carried so the fire path floors the Kalshi limit to a legal tick. Cross-pair
        # overrides trade a different ticker in the same series → same linear_cent tick.
        kalshi_tick=pair.kalshi_market.price_tick,
        # The Poly market's own tick, for the log only. Both legs' ticks are recorded because they
        # differ per MARKET (not per series) — a difference that silently flipped the Poly buffer
        # between a real cushion and a no-op until the buffer was dropped (S4 → COMPLETED.md), and
        # that still decides which rungs poly_fillable sums. Values: venue-reference skill.
        poly_tick=pair.poly_pair.poly_tick,
        # Kickoff (= Poly start_date / gameStartTime) for the log's minutes-since-kickoff column.
        kickoff=pair.poly_pair.start_date,
    )


def _find_cross_pair_directions(
    pairs: list[CrossPlatformPair],
    poly_feed, kalshi_feed,
    min_e: float,
    poly_balance: float | None,
    kalshi_balance: float | None,
) -> list[KalshiArbOpportunity]:
    """Cross-pair directions (two-outcome sports only). For MLB/NBA/NHL each game produces two
    single-team CrossPlatformPairs (one per team); in a two-outcome sport, buying Poly A YES +
    Kalshi B YES also pays exactly $1, so we check those directions too. Soccer (KXWCGAME) is
    excluded: a draw would leave both YES legs losing. Pure — reads the feeds, returns the extra
    candidate opportunities (the caller extends its candidate list with these)."""
    from collections import defaultdict
    out: list[KalshiArbOpportunity] = []
    st_groups: dict[str, list[CrossPlatformPair]] = defaultdict(list)
    for pair in pairs:
        is_single = pair.kalshi_yes_team.lower() == pair.kalshi_no_team.lower()
        series    = pair.kalshi_market.event_ticker.split("-")[0]
        if is_single and series in _TWO_OUTCOME_SERIES:
            st_groups[pair.poly_pair.event_id].append(pair)

    for event_id, group in st_groups.items():
        if len(group) != 2:
            continue
        # Short Kalshi labels vs full Poly names → match by name resolver, not
        # exact compare (else MLB pairs never group and the cross-directions skip).
        pair_for_a = next(
            (p for p in group if _names_match(p.poly_team_a, p.kalshi_yes_team, {})), None
        )
        pair_for_b = next(
            (p for p in group if _names_match(p.poly_team_b, p.kalshi_yes_team, {})), None
        )
        if pair_for_a is None or pair_for_b is None:
            continue

        poly_pair     = pair_for_a.poly_pair
        poly_fee_rate = poly_pair.taker_fee_rate

        # Dir 3: Poly team_A YES + Kalshi team_B YES
        p_data_a = poly_feed.get_price(poly_pair.token_yes_a)
        k_ask_b  = kalshi_feed.get_best_ask(pair_for_b.kalshi_market.ticker, "yes")
        if p_data_a is not None and k_ask_b is not None:
            edge3 = check_kalshi_arb(p_data_a.best_ask, k_ask_b, poly_fee_rate)
            if edge3 >= min_e:
                out.append(
                    _size_kalshi_opportunity(
                        pair_for_a, "a", "yes",
                        p_data_a.best_ask, k_ask_b, poly_fee_rate, edge3,
                        poly_ask_depth=getattr(p_data_a, "ask_depth", 0.0),
                        poly_balance=poly_balance, kalshi_balance=kalshi_balance,
                        kalshi_ticker_override=pair_for_b.kalshi_market.ticker,
                        kalshi_team_override=pair_for_b.kalshi_yes_team,
                    )
                )

        # Dir 4: Poly team_B YES + Kalshi team_A YES
        p_data_b = poly_feed.get_price(poly_pair.token_yes_b)
        k_ask_a  = kalshi_feed.get_best_ask(pair_for_a.kalshi_market.ticker, "yes")
        if p_data_b is not None and k_ask_a is not None:
            edge4 = check_kalshi_arb(p_data_b.best_ask, k_ask_a, poly_fee_rate)
            if edge4 >= min_e:
                out.append(
                    _size_kalshi_opportunity(
                        pair_for_a, "b", "yes",
                        p_data_b.best_ask, k_ask_a, poly_fee_rate, edge4,
                        poly_ask_depth=getattr(p_data_b, "ask_depth", 0.0),
                        poly_balance=poly_balance, kalshi_balance=kalshi_balance,
                        kalshi_ticker_override=pair_for_a.kalshi_market.ticker,
                        kalshi_team_override=pair_for_a.kalshi_yes_team,
                    )
                )
    return out


def find_kalshi_arbs(
    pairs: list[CrossPlatformPair],
    poly_feed,           # OrderBookCache
    kalshi_feed,         # KalshiOrderBookCache
    min_edge: float | None = None,
    poly_balance: float | None = None,    # cap leg size at affordable Poly funds (None = no cap)
    kalshi_balance: float | None = None,  # cap leg size at affordable Kalshi funds (None = no cap)
    dominated_out: list | None = None,    # if given, appended with the per-event losers
) -> list[KalshiArbOpportunity]:
    """
    Detect cross-platform arbs between Polymarket and Kalshi.

    For each CrossPlatformPair, checks two complementary directions:
      Dir 1: buy poly token_yes_a + buy kalshi complement (pays if team_b wins)
      Dir 2: buy poly token_yes_b + buy kalshi complement (pays if team_a wins)

    Returns every DISTINCT fireable direction (deduped by exact position —
    poly_token + kalshi ticker/side), sorted by edge descending. Multiple directions
    on one game each fire as their own hedged arb, bounded by the exposure caps.
    """
    min_e = min_edge if min_edge is not None else config.KALSHI_ARB_MIN_EDGE
    _detected_ts = time.time()  # this-loop detection instant (sync fn, no awaits) → stamped on every
    # returned opp for fill_success.csv's fill-window latency. Observability only; never gates.
    candidates: list[KalshiArbOpportunity] = []

    for pair in pairs:
        poly_fee_rate = pair.poly_pair.taker_fee_rate
        ticker        = pair.kalshi_market.ticker

        # Determine which Kalshi side aligns with each Poly team. Use the same
        # name matcher the pairing used — Kalshi labels are short ("Toronto")
        # while Poly names are full ("Toronto Blue Jays"), so an exact compare
        # would mis-assign the side for MLB. _names_match resolves both.
        if _names_match(pair.poly_team_a, pair.kalshi_yes_team, {}):
            kalshi_a_side = "yes"
            kalshi_b_side = "no"
        else:
            kalshi_a_side = "no"
            kalshi_b_side = "yes"

        # Single-team markets (soccer): yes_team == no_team means this Kalshi
        # market is "team X wins YES / team X doesn't win NO". Only one direction
        # is valid per CrossPlatformPair — the one where we buy Poly YES for the
        # matching team AND Kalshi NO. The opposite direction (Poly YES_other +
        # Kalshi YES_team) is invalid for 3-outcome games because a tie pays
        # neither leg.
        is_single_team = pair.kalshi_yes_team.lower() == pair.kalshi_no_team.lower()

        yes_is_team_a = _names_match(pair.poly_team_a, pair.kalshi_yes_team, {})

        # Direction 1: buy poly YES_A + buy kalshi complement for team_b.
        # For single-team soccer markets, valid only when Kalshi YES = poly team_a.
        if not is_single_team or yes_is_team_a:
            p_data_a = poly_feed.get_price(pair.poly_pair.token_yes_a)
            k_ask_b  = kalshi_feed.get_best_ask(ticker, kalshi_b_side)
            if p_data_a is not None and k_ask_b is not None:
                edge1 = check_kalshi_arb(p_data_a.best_ask, k_ask_b, poly_fee_rate)
                if edge1 >= min_e:
                    candidates.append(
                        _size_kalshi_opportunity(
                            pair, "a", kalshi_b_side,
                            p_data_a.best_ask, k_ask_b, poly_fee_rate, edge1,
                            poly_ask_depth=getattr(p_data_a, "ask_depth", 0.0),
                            poly_balance=poly_balance, kalshi_balance=kalshi_balance,
                        )
                    )

        # Direction 2: buy poly YES_B + buy kalshi complement for team_a.
        # For single-team soccer markets, valid only when Kalshi YES = poly team_b.
        if not is_single_team or not yes_is_team_a:
            p_data_b = poly_feed.get_price(pair.poly_pair.token_yes_b)
            k_ask_a  = kalshi_feed.get_best_ask(ticker, kalshi_a_side)
            if p_data_b is not None and k_ask_a is not None:
                edge2 = check_kalshi_arb(p_data_b.best_ask, k_ask_a, poly_fee_rate)
                if edge2 >= min_e:
                    candidates.append(
                        _size_kalshi_opportunity(
                            pair, "b", kalshi_a_side,
                            p_data_b.best_ask, k_ask_a, poly_fee_rate, edge2,
                            poly_ask_depth=getattr(p_data_b, "ask_depth", 0.0),
                            poly_balance=poly_balance, kalshi_balance=kalshi_balance,
                        )
                    )

    # ── Cross-pair directions (two-outcome sports only) — extracted for legibility ─────────
    candidates.extend(_find_cross_pair_directions(
        pairs, poly_feed, kalshi_feed, min_e, poly_balance, kalshi_balance))

    # Keep every DISTINCT fireable direction. A position is identified by its exact legs
    # (poly_token + kalshi ticker/side); different directions on the same game are
    # independent hedged arbs and each fires on its own, capped by total exposure. We
    # dedupe ONLY exact-duplicate positions (the same crossing surfaced via two
    # overlapping pairs) — keep the higher edge, surface the dropped twin via
    # dominated_out. (Was one-best-per-event, which discarded fireable lower-edge
    # directions; those are now captured.)
    best: dict[tuple, KalshiArbOpportunity] = {}
    for opp in candidates:
        key = (opp.poly_token, opp.kalshi_ticker, opp.kalshi_side)
        cur = best.get(key)
        if cur is None:
            best[key] = opp
        elif opp.edge > cur.edge:
            if dominated_out is not None:
                dominated_out.append(cur)      # exact-dup position, lower edge dropped
            best[key] = opp
        elif dominated_out is not None:
            dominated_out.append(opp)          # exact-dup position, this one is lower edge

    result = sorted(best.values(), key=lambda o: o.edge, reverse=True)
    for o in result:
        o.detected_ts = _detected_ts
    return result


def _size_macro_opportunity(
    pair: "MacroPair",
    poly_ask_raw: float,
    kalshi_ask: float,
    poly_fee_rate: float,
    edge: float,
) -> KalshiArbOpportunity:
    """Build a KalshiArbOpportunity from a MacroPair using the same sizing math
    as _size_kalshi_opportunity. poly_event_id := poly_condition_id (cooldown key).
    """
    poly_effective   = _effective_share_cost(poly_ask_raw, poly_fee_rate)
    kalshi_effective = kalshi_ask + _kalshi_taker_fee(kalshi_ask)

    # Size = the per-trade budget over the DEARER leg. No edge multiplier: CROSS_ARB_SIZE_TIERS
    # was deleted 2026-07-15 (its 0.005/0.01 breakpoints sat below the 0.02 fire floor, so it was a
    # constant 1.0 on every live path). Do NOT re-add edge-scaled sizing without re-tuning ABOVE
    # the floor — and never size UP on fat edges: cross-venue, a fat edge means the two venues
    # DISAGREE, i.e. one is stale, which is peak strand risk. See docs/TODO.md S1.
    position_usd = config.MAX_POSITION_USD
    shares       = max(1, math.floor(position_usd / max(poly_effective, kalshi_effective)))

    total_cost        = shares * (poly_effective + kalshi_effective)
    guaranteed_profit = shares * 1.0 - total_cost

    return KalshiArbOpportunity(
        event_title=f"MACRO {pair.category.upper()} {pair.kalshi_ticker}",
        poly_event_id=pair.poly_condition_id,
        poly_token=pair.poly_token,
        poly_team=pair.category,
        poly_ask_raw=poly_ask_raw,
        poly_ask=poly_effective,
        kalshi_ticker=pair.kalshi_ticker,
        kalshi_side=pair.kalshi_side,
        kalshi_team=pair.kalshi_side,
        kalshi_ask=kalshi_ask,
        edge=edge,
        shares=shares,
        total_cost=total_cost,
        guaranteed_profit=guaranteed_profit,
    )


def find_macro_arbs(
    pairs: "list[MacroPair]",
    poly_feed,
    kalshi_feed,
    min_edge: float | None = None,
) -> list[KalshiArbOpportunity]:
    """Detect cross-platform arbs for the macro allowlist (Fed/CPI).

    For each MacroPair reads poly_token's ask and kalshi_ticker/kalshi_side's ask,
    gates on both quotes being present, and emits one KalshiArbOpportunity when
    edge >= min_edge. kalshi_side alone drives which Kalshi price is read —
    complementarity is operator-asserted via MacroPair.comment.
    """
    from bot.kalshi.macro_pairs import _MACRO_FEE_RATES  # lazy: avoids import cycle

    min_e = min_edge if min_edge is not None else config.KALSHI_MACRO_MIN_EDGE
    candidates: list[KalshiArbOpportunity] = []

    for pair in pairs:
        poly_fee_rate = _MACRO_FEE_RATES.get(pair.category, 0.0)

        p_data = poly_feed.get_price(pair.poly_token)
        k_ask  = kalshi_feed.get_best_ask(pair.kalshi_ticker, pair.kalshi_side)
        if p_data is None or k_ask is None:
            continue

        edge = check_kalshi_arb(p_data.best_ask, k_ask, poly_fee_rate)
        if edge >= min_e:
            candidates.append(
                _size_macro_opportunity(pair, p_data.best_ask, k_ask, poly_fee_rate, edge)
            )

    candidates.sort(key=lambda o: o.edge, reverse=True)
    return candidates


def find_kalshi_tightest(
    pairs: list[CrossPlatformPair],
    poly_feed,
    kalshi_feed,
) -> Optional[KalshiArbOpportunity]:
    """Return the best-edge pair across all matched pairs, regardless of min_edge threshold.

    Used for diagnostic logging to show how close prices are even when no arb exists.
    """
    results = find_kalshi_arbs(pairs, poly_feed, kalshi_feed, min_edge=-999.0)
    return results[0] if results else None
