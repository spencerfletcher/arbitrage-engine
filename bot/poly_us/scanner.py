"""
bot/poly_us_scanner.py
──────────────────────
Discovers open Polymarket US markets for a set of sport series IDs and emits
MarketPair objects compatible with the existing Kalshi matcher.

Poly US identifies markets by slug, not token ID. We place the orderable
market-side "identifier" slugs into MarketPair.token_* fields; the matcher
treats those as opaque strings, and PolyUSClient interprets them as slugs when
placing orders. Discovery keys off seriesId (tagId filtering is broken on the
public endpoint). seriesId per sport comes from sports.list().
"""
from __future__ import annotations

from bot.core.types import MarketPair
from bot.core.logger import get_logger
from bot.poly_us.sides import short_token

log = get_logger(__name__)


# Only this market type is the 3-way "winner (w/ tie)" structure where a draw
# settles a Draw contract (team YES → $0, not $0.50) on REGULATION time — the
# exact complement of Kalshi's regulation-time NO. Knockout / "w/o tie" markets
# resolve on extra time / penalties (per-fixture Contract Terms, undocumented
# generally), so the cross-venue $1 hedge is NOT guaranteed there. Trade only
# drawable (group-stage) markets.
_DRAWABLE_TYPE = "drawable_outcome"
# Poly US RENAMED the WC winner type "drawable_outcome" → "soccer_team_full_time_winner"
# (~2026-06-24, which silently dropped ALL World Cup matching → 0 cross-pairs, the KXWCGAME
# schema-drift alert). Same 3-outcome structure (team-A / draw / team-B). Accept it so matching
# resumes, mirroring the MLB "moneyline" → "baseball_team_full_game_winner" rename handling.
# ⚠️ SETTLEMENT-EQUIVALENCE ASSUMPTION — UNVERIFIED, BLOCKS SHIP: this admits the type into the
# cross-venue hedge ONLY IF it settles 90' REGULATION (no ET/penalties), the same basis as
# drawable_outcome and Kalshi's KXWCGAME NO. The name "full_time" SUGGESTS regulation, and live
# data shows it on 3-outcome (drawable) games — but that is INFERENCE, not Poly's contract. Do NOT
# ship until verified against Poly's published Contract Terms. If it is ever applied to a knockout
# fixture (ET/pens), the $1 hedge breaks. See docs/g3 self-review.
_SOCCER_FULL_TIME_TYPE = "soccer_team_full_time_winner"
# Two-outcome (no-tie) winner market — MLB/NBA/NHL. ONE binary slug per game:
# long side = team A, short side = team B. Baseball can't draw, so the cross-venue
# hedge (Poly team-X YES + Kalshi team-X NO) pays a guaranteed $1 — settlement is
# cleaner than soccer (no draw to exclude). Whitelisted per series in the matcher.
# This is the ECONOMIC settlement label stamped on the MarketPair (matched by the
# settlement-equivalence allowlist's KXMLBGAME key) — NOT the Poly API type string.
_MONEYLINE_TYPE = "moneyline"


def _is_moneyline_market_type(mt) -> bool:
    """True for a full-game WINNER (moneyline) market by its Poly `sportsMarketType`.

    Poly US RENAMED MLB's winner type from "moneyline" → "baseball_team_full_game_winner"
    (~2026-06-23, which silently stopped all MLB matching). Accept the legacy name AND the
    "<sport>_team_full_game_winner" pattern so the same rename on NBA/NHL can't bite us again.
    Deliberately NOT *_first_five_winner / *_spread / *_total — those aren't settlement-equivalent
    to Kalshi's full-game winner (and the downstream settlement-equivalence gate is the 2nd guard)."""
    return mt == _MONEYLINE_TYPE or (isinstance(mt, str) and mt.endswith("_team_full_game_winner"))


def _is_drawable_market_type(mt) -> bool:
    """True for the 3-way regulation-time "winner (w/ tie)" structure by Poly `sportsMarketType`.

    Accepts the legacy "drawable_outcome" AND the post-rename "soccer_team_full_time_winner"
    (~2026-06-24 — see _SOCCER_FULL_TIME_TYPE). Both carry the same team-A/draw/team-B structure;
    the pair is still stamped settlement_type=_DRAWABLE_TYPE (the ECONOMIC label), so the
    settlement-equivalence allowlist key ("KXWCGAME", "drawable_outcome") gates it WITHOUT change.
    ⚠️ Admitting the renamed type assumes it settles 90' regulation (no ET/pens) — UNVERIFIED,
    blocks ship until confirmed against Poly Contract Terms (see _SOCCER_FULL_TIME_TYPE)."""
    return mt == _DRAWABLE_TYPE or mt == _SOCCER_FULL_TIME_TYPE
# Poly US taker fee: **fee = round(Θ·C·p·(1−p), 2)** — parabolic, rounded to the CENT on the
# ORDER TOTAL. [VERIFIED 2026-07-15 against 9 REAL exchange commissions read back from
# /v1/order/{id} + portfolio/activities — pinned in tests/test_poly_fee_model.py. Not from docs.]
#
# ⚠️ Θ IS NOT CONSTANT — Poly RAISED it 0.05 → 0.06 between 2026-06-17 and 2026-07-15 (+20%).
# Proof: June fills fit Θ=0.05 EXACTLY (7/7), 2026-07-15 fills fit Θ=0.06 EXACTLY (2/2), and every
# live market now reports feeCoefficient=0.06. This is why the scanner reads `feeCoefficient`
# PER-MARKET instead of trusting this constant — and that design WORKED: the live path
# self-corrected through the change with no code edit (all 29 live pairs price at 0.06 today).
#
# This value is the FALLBACK ONLY, for when the venue doesn't report feeCoefficient. Keep it at
# the venue's CURRENT coefficient: the fallback is SILENT (a rename → no error, no alert), and a
# stale-LOW Θ understates the fee → OVERSTATES every edge (~0.25¢/share at p=0.5 ≈ 12% of the 2%
# floor) → fires trades that were never above the floor. Stale-low fails the DANGEROUS way, so if
# in doubt round this UP, never down.
_POLY_US_TAKER_THETA = 0.06

# Human-readable labels for the numeric Poly US series IDs (sports.list()), so
# the discovery summary reads "WorldCup=58 MLB=14" instead of "69=58 15=14".
_SERIES_NAMES: dict[str, str] = {"69": "WorldCup", "15": "MLB", "4": "NBA", "6": "NHL", "49": "WNBA"}

# Poly US seriesId → the Kalshi series ticker for the SAME sport. Stamped onto each MarketPair so
# the cross-venue matcher only pairs same-sport games (a Poly MLB "Minnesota" must not match a
# Kalshi WNBA "Minnesota" — different games → phantom edge). An unmapped sid → "" → fail-closed
# (the pair matches no Kalshi market, surfacing as 0 crosspairs rather than a cross-sport mispair).
_POLY_SERIES_TO_KALSHI: dict[str, str] = {
    "69": "KXWCGAME", "15": "KXMLBGAME", "4": "KXNBAGAME", "6": "KXNHLGAME", "49": "KXWNBAGAME",
}


def _series_label(sid: str) -> str:
    return _SERIES_NAMES.get(sid, sid)


def _team_yes_sides(event: dict) -> list[tuple[str, str]]:
    """Return [(team_name, market_side_identifier), ...] for each team's YES/long side.

    Only drawable (w/-tie, regulation-time) markets are included — see _DRAWABLE_TYPE.
    """
    sides: list[tuple[str, str]] = []
    for market in event.get("markets", []):
        if not _is_drawable_market_type(market.get("sportsMarketType")):
            continue  # skip knockout / w/o-tie markets — hedge not settlement-safe
        for side in market.get("marketSides", []):
            if side.get("long") is True and (side.get("description") or "").lower() == "yes":
                team = (side.get("team") or {}).get("name")
                ident = side.get("identifier")
                if team and ident:
                    sides.append((team, ident))
    return sides


def _moneyline_team_sides(event: dict) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Return ((long_team, slug), (short_team, slug)) for a moneyline event, or None.

    A moneyline game is ONE market with two marketSides sharing one slug:
    long=True is team A, long=False is team B. Both legs trade off that slug.
    """
    for market in event.get("markets", []):
        if not _is_moneyline_market_type(market.get("sportsMarketType")):
            continue
        long_side = short_side = None
        for side in market.get("marketSides", []):
            team = (side.get("team") or {}).get("name")
            ident = side.get("identifier")
            if not team or not ident:
                continue
            if side.get("long") is True:
                long_side = (team, ident)
            elif side.get("long") is False:
                short_side = (team, ident)
        if long_side and short_side:
            return long_side, short_side
    return None


def _coerce_fee_rate(raw) -> float:
    """A market's `feeCoefficient` as a float, or the documented Θ default.

    Fail-safe DIRECTION (money path): a missing / null / non-numeric / non-positive value falls
    back to `_POLY_US_TAKER_THETA`, NEVER 0 — a zero fee would under-cost every Poly edge and
    over-size the position. Reading it live means a venue fee change self-corrects instead of
    silently mis-costing against the hardcoded assumption."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _POLY_US_TAKER_THETA
    return v if v > 0 else _POLY_US_TAKER_THETA


def _event_fee_rate(event: dict, type_pred) -> float:
    """`feeCoefficient` of the first market whose `sportsMarketType` satisfies `type_pred`,
    read-with-fallback. `type_pred` is a callable so it tracks the same (renamed) winner market
    the pair is built from — else MLB would silently fall back to the default fee."""
    for market in event.get("markets", []):
        if type_pred(market.get("sportsMarketType")):
            return _coerce_fee_rate(market.get("feeCoefficient"))
    return _POLY_US_TAKER_THETA


def _event_poly_tick(event: dict, type_pred) -> float | None:
    """`orderPriceMinTickSize` of the first market matching `type_pred` — the market's minimum
    price increment. LOGGING-ONLY (would_fire.csv); no gate or price computation reads it.

    Fails to None, NOT to a default — the OPPOSITE direction from _coerce_fee_rate next door, and
    deliberately so. A fee needs a number to cost the trade with, so it falls back rather than
    under-cost. Nothing needs this one, and a default would be right often enough to LOOK correct
    while quietly misreporting the markets that differ — which is most of the point, since the tick
    varies within a series (MLB 0.005, NBA/WNBA 0.01, WC both — VERIFIED 2026-07-16). The value
    this column was defended against is exactly the one it found: every doc said MLB was 0.01.
    Blank means unread; it never means a default.

    The venue sends a BARE NUMBER (`0.01`), not the {value,currency} Amount its neighbouring
    price fields use [VERIFIED 2026-07-15 against the live event feed, WorldCup drawable_outcome].
    The Amount branch below is defensive, not observed — the fields around this one ARE wrapped,
    so the venue moving this one is plausible drift, and it would otherwise fail as a permanent
    silent blank rather than an error."""
    for market in event.get("markets", []):
        if type_pred(market.get("sportsMarketType")):
            raw = market.get("orderPriceMinTickSize")
            if isinstance(raw, dict):
                raw = raw.get("value")
            try:
                tick = float(raw)
            except (TypeError, ValueError):
                return None
            return tick if tick > 0 else None
    return None


def _event_to_pair(event: dict, sid: str) -> MarketPair | None:
    """Build a binary MarketPair from an event, or None. `sid` is the Poly seriesId being scanned,
    used to stamp the pair's expected Kalshi series (the sport-consistency gate, see kalshi_series).

    Tries the soccer drawable (3-way) structure first, then the two-outcome
    (moneyline) structure. Leaves drawable handling unchanged.
    """
    if event.get("closed"):
        return None
    kalshi_series = _POLY_SERIES_TO_KALSHI.get(sid, "")

    sides = _team_yes_sides(event)
    if len(sides) >= 2:
        (team_a, ident_a), (team_b, ident_b) = sides[0], sides[1]
        return MarketPair(
            event_id=str(event.get("slug", "")),
            event_title=event.get("title", "Unknown"),
            token_yes_a=ident_a,
            token_no_a=ident_b,
            question_a=f"{team_a} wins",
            token_yes_b=ident_b,
            token_no_b=ident_a,
            question_b=f"{team_b} wins",
            start_date=event.get("startTime") or event.get("startDate"),
            end_date=event.get("endTime") or event.get("endDate"),  # active-game-window END (drawable path)
            # Poly US charges a taker fee (Θ·p·(1-p)); model it so edges are net,
            # not gross. Without this the bot overstates every Poly US edge. Θ is read
            # live from the market's feeCoefficient (fallback _POLY_US_TAKER_THETA).
            taker_fee_rate=_event_fee_rate(event, _is_drawable_market_type),
            poly_tick=_event_poly_tick(event, _is_drawable_market_type),
            # Sides come from a drawable-type market (legacy drawable_outcome OR the renamed
            # soccer_team_full_time_winner — see _is_drawable_market_type). The settlement label
            # stays _DRAWABLE_TYPE (the ECONOMIC type) so the ("KXWCGAME","drawable_outcome")
            # allowlist key gates without expansion — the rename is a Poly API string change, not
            # an economic one. (Pending the regulation-time verification in _SOCCER_FULL_TIME_TYPE.)
            settlement_type=_DRAWABLE_TYPE,
            kalshi_series=kalshi_series,
        )

    ml = _moneyline_team_sides(event)
    if ml is not None:
        (long_team, slug), (short_team, _slug) = ml
        # One slug, two sides: long → token_yes_a, short → "<slug>::short" so the
        # detector/feed/client route each team independently (see bot/poly_us/sides).
        return MarketPair(
            event_id=str(event.get("slug", "")),
            event_title=event.get("title", "Unknown"),
            token_yes_a=slug,
            token_no_a=short_token(slug),
            question_a=f"{long_team} wins",
            token_yes_b=short_token(slug),
            token_no_b=slug,
            question_b=f"{short_team} wins",
            start_date=event.get("startTime") or event.get("startDate"),
            end_date=event.get("endTime") or event.get("endDate"),  # active-game-window END (moneyline path)
            taker_fee_rate=_event_fee_rate(event, _is_moneyline_market_type),
            poly_tick=_event_poly_tick(event, _is_moneyline_market_type),
            settlement_type=_MONEYLINE_TYPE,
            kalshi_series=kalshi_series,
        )

    return None


class PolyUSScanner:
    """Fetches open Polymarket US markets for a list of sport series IDs."""

    def __init__(self, sdk) -> None:
        self._sdk = sdk
        # Last per-series breakdown logged at INFO; lets us re-log only when the
        # mix changes (e.g. MLB 0→14 when the day's games open) instead of every scan.
        self._last_breakdown: dict[str, int] | None = None
        # Raw EVENTS fetched per Kalshi-series key on the last scan (NOT pairs built). Read by the
        # schema-drift detector to tell a parse/rename break (events>0, 0 pairs) from a between-slates
        # lull (0 events). Errored series are omitted (an error ≠ "0 events" → fail-quiet).
        self.last_raw_event_counts: dict[str, int] = {}

    _PAGE_SIZE = 100  # events.list defaults to ~20/page; paginate so we don't drop games

    async def fetch_markets(self, series_ids: list[str]) -> list[MarketPair]:
        pairs: list[MarketPair] = []
        breakdown: dict[str, int] = {}
        raw_counts: dict[str, int] = {}
        for sid in series_ids:
            try:
                count = 0
                n_pairs = 0
                offset = 0
                # Paginate until a short page: a series (e.g. World Cup) can have
                # 100s of events; the default page would silently drop most of them.
                while True:
                    resp = await self._sdk.events.list(params={
                        "seriesId": sid, "active": True, "closed": False,
                        "limit": self._PAGE_SIZE, "offset": offset,
                    })
                    events = resp.get("events", []) if isinstance(resp, dict) else []
                    for event in events:
                        pair = _event_to_pair(event, sid)
                        if pair:
                            pairs.append(pair)
                            n_pairs += 1
                    count += len(events)
                    if len(events) < self._PAGE_SIZE:
                        break
                    offset += self._PAGE_SIZE
                breakdown[sid] = n_pairs
                ks = _POLY_SERIES_TO_KALSHI.get(sid, "")   # key by Kalshi series for the drift detector
                if ks:
                    raw_counts[ks] = count                 # raw events fetched (not pairs built)
                log.debug(f"PolyUSScanner: seriesId={sid} → {count} events")
            except Exception as exc:
                log.error(f"PolyUSScanner: error fetching seriesId={sid}: {exc}")
        self.last_raw_event_counts = raw_counts
        self._log_breakdown(len(pairs), breakdown)
        return pairs

    def _log_breakdown(self, total: int, breakdown: dict[str, int]) -> None:
        """Log per-series market counts — INFO when the mix changes, else DEBUG.

        Only successfully-fetched series appear in ``breakdown``; a series that
        errored out is omitted (an error ≠ "0 markets") and so won't flip the mix.
        """
        summary = " ".join(f"{_series_label(s)}={n}" for s, n in breakdown.items())
        msg = (
            f"PolyUSScanner: {total} binary markets across "
            f"{len(breakdown)} series ({summary})"
        )
        if breakdown != self._last_breakdown:
            log.info(msg)
            self._last_breakdown = breakdown
        else:
            log.debug(msg)
