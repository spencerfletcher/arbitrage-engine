"""
bot/kalshi_matcher.py
─────────────────────
Matches Polymarket MarketPair objects to Kalshi KalshiMarket objects.

Matching strategy (mirrors bot/matcher.py):
  1. Time filter: |poly.start_date - kalshi.close_time| < 4h
     (Poly start_date = game start; Kalshi close_time = game end — wider window)
  2. Name match: each Kalshi side (yes_label, no_label) against each Poly team
     using the same 3-strategy cascade from matcher.py
  3. Both sides must match — one to YES, one to NO

Returns CrossPlatformPair with resolved team assignments on each platform.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.kalshi.scanner import KalshiMarket
from bot.core.logger import get_logger
from bot.core.matcher import _extract_team_name, _names_match, parse_iso
from bot.core.types import MarketPair

log = get_logger(__name__)

# ── Settlement-equivalence allowlist (single source of truth) ─────────────────
# A cross-exchange hedge (Poly-YES + Kalshi-NO) pays a guaranteed $1 ONLY if both
# legs settle on identical outcome sets. This allowlist names (Kalshi series ×
# Poly settlement structure) pairs VERIFIED equivalent. Fail-closed: anything not
# listed is blocked. Do NOT widen without confirming both venues' published rules.
#
# NOTE (public snapshot): the entries below are the publicly-verifiable major-league
# pairs and are what this open showcase runs on. Any additional / experimental
# equivalence pairs used in production are maintained privately and loaded at runtime
# from an optional overlay (see NOTICE.md); absent here, only the pairs below are active.
# The macro (Fed/CPI) pairs follow the same pattern — see bot/kalshi/macro_pairs.py,
# whose MACRO_PAIRS list ships empty by design.
#
# Verified 2026-06-17:
#   WC game markets — both venues settle on 90'+stoppage, EXCLUDE extra time /
#   penalties, and a draw is a valid outcome for the drawable (w/-tie) structure.
#   → ("KXWCGAME", "drawable_outcome") is equivalent (group stage). Knockout Poly
#   markets settle on the final winner (incl ET/pens) and are NOT drawable_outcome,
#   so they never match this entry.
#   MLB game markets — Kalshi KXMLBGAME settles on the game winner INCLUDING extra
#   innings; Poly US "moneyline" is the full-game winner (no draw possible in
#   baseball). Both settle on the same final result → ("KXMLBGAME", "moneyline")
#   is equivalent. Cleaner than soccer: no draw to exclude. NBA/NHL not verified.
# TWO SEPARATE UNGATED TAILS (do not conflate):
#  (1) VOID/cancellation → each venue settles to a DISCRETIONARY FAIR-VALUE mark, NOT
#      last-traded: Poly "Last Fair Market Price" (LFMP), Kalshi Rule 6.3(c) "last
#      traded fair price". The two marks need not agree → bounded loss. (Corrected: an
#      earlier comment said "last-traded" — wrong; it's a fair-value estimate.)
#  (2) DIVERGENCE → the venues declare OPPOSITE definite results for the same game
#      (the documented Super Bowl case: Kalshi YES@0.26 vs Polymarket YES@1.00). This
#      BREAKS the hedge — both legs can pay $0 → ~full-stake loss. Distinct from void.
#   Group-stage drawable suppresses divergence (90'-scoreline is objective) but does
#   not eliminate it (late VAR reversal, data-source split). Both are rare and ungated
#   in v1; they're priced pessimistically in scripts/settlement_backtest.py, not here.
#   MLB rules verified 2026-06-17: normal/≤2-day-postponed → winner ✓; cancelled →
#   both fair-value marks; 2d–2wk postponement → Kalshi marks, Poly pays real winner
#   (non-complementary); tie ≈impossible.
_SETTLEMENT_EQUIVALENT: frozenset[tuple[str, str]] = frozenset({
    ("KXWCGAME", "drawable_outcome"),
    ("KXMLBGAME", "moneyline"),
    # WNBA: verified 2026-06-23 from live rules text on both venues — both settle on the official
    # game winner (OT included, no draw possible), same scheduled game; void → fair-value on both
    # (Poly LFMP / Kalshi fair mark), the standard void path. Cleaner than the WC soccer entry (no
    # draw, no ET/penalties, no source-disagreement risk). Live per-game markets confirmed.
    ("KXWNBAGAME", "moneyline"),
    # NBA: allowlisted on the SAME basis as WNBA (identical league class — winner incl OT, no draw,
    # symmetric void). Seasonally INACTIVE as of 2026-06 (no live games until ~October); contributes
    # zero data rate now, added for allowlist coherence + so it just works next season with no
    # rediscovery. Resolves the prior incoherence of WNBA-allowed / NBA-blocked on identical grounds.
    ("KXNBAGAME", "moneyline"),
    # NHL: verified 2026-07-14 from BOTH venues' rules text (Kalshi settled market + Poly closed
    # market, since NHL is off-season). Kalshi: "If CAR Hurricanes wins the ... professional hockey
    # game scheduled for Jun 14, 2026, then the market resolves to Yes." Poly (moneyline): "Who will
    # win on the upcoming ice hockey game ... postponed → market remains open until the game has been
    # completed; canceled entirely, with no make-up game → resolve 50-50." Same winner basis, and NHL
    # CANNOT draw (5-min OT then shootout always decide, since 2005-06; playoffs = sudden-death OT) —
    # the same no-draw class as NBA/WNBA. Poly's NHL template is VERBATIM the NBA one.
    # ⚠️ Void paths are NOT uniform across this allowlist — the WNBA note above ("void → fair-value on
    # both ... the standard void path") overstates it, and that claim was checked and is wrong as a
    # generalisation: NBA+NHL → 50-50, WNBA → last fair market price, MLB → tie $0.50 / last-traded
    # if rescheduled >2wk. Each entry carries its own void residual vs Kalshi's mark. All trigger only
    # on outright cancellation with no make-up (~0 rate: 0 voids observed in 408 settled games), which
    # is why this is accepted, NOT because the paths match. Seasonally INACTIVE until ~Oct (zero data
    # rate now); already wired in config (series 6) + scanner mapping, so only this entry gated it.
    ("KXNHLGAME", "moneyline"),
})

# Optional private overlay: production/experimental equivalence pairs kept out of this
# public snapshot are merged in if the (gitignored) overlay module is present. Absent →
# no-op, only the public pairs above are active. See NOTICE.md.
try:  # pragma: no cover — overlay is optional and absent in the public tree
    from bot.kalshi._private_pairs import SETTLEMENT_EQUIVALENT_EXTRA  # type: ignore
    _SETTLEMENT_EQUIVALENT = _SETTLEMENT_EQUIVALENT | frozenset(SETTLEMENT_EQUIVALENT_EXTRA)
except Exception:  # noqa: BLE001 — optional overlay, never fatal
    pass


def is_settlement_equivalent(kalshi_ticker: str, poly_settlement_type: str) -> bool:
    """True only if this (Kalshi series, Poly settlement structure) is a verified
    settlement-equivalent pair. The Kalshi series is the ticker prefix before the
    first '-' (e.g. 'KXWCGAME-26JUN17GHAPAN-GHA' → 'KXWCGAME')."""
    if not kalshi_ticker:
        return False
    series = kalshi_ticker.split("-", 1)[0]
    return (series, poly_settlement_type) in _SETTLEMENT_EQUIVALENT


@dataclass
class CrossPlatformPair:
    poly_pair: MarketPair
    kalshi_market: KalshiMarket
    poly_team_a: str        # team from poly question_a (e.g. "Lakers")
    poly_team_b: str        # team from poly question_b (e.g. "Celtics")
    kalshi_yes_team: str    # team that YES pays on Kalshi
    kalshi_no_team: str     # team that NO pays on Kalshi


# Kalshi's expected_expiration_time sits EXACTLY 3.0h after the game start on every series we
# trade [VERIFIED 2026-07-16 against the live feed: 54/54 open MLB markets measured against the
# start encoded in their own tickers, and 4/4 live matched WC pairs measured against Poly's start
# — one value, 3.00h, no spread]. Note it is NOT game length: a ~2h World Cup match and a ~3h
# baseball game both get +3.0h, so this is a Kalshi constant. That is why NBA/NHL are likely fine
# unmeasured (see below) — and why this window only has to absorb the two venues' disagreement
# about kickoff, not anything about the sport.
#
# The bounds are set by a PHYSICAL constraint, not a guess. A doubleheader's game 2 cannot start
# before game 1 ends, so its start is >3.0h after game 1's, and its expiry is >6.0h after game 1's
# START. Meanwhile a legitimate match is 3.0h flat. So:
#     legit          = 3.0h exactly
#     wrong game     > 6.0h   (game gap > 3.0h, forced, + the 3.0h offset)
# Anything in (3.0, 6.0) separates them. 6.0 would sit exactly ON the boundary — a zero-turnaround
# doubleheader lands at 6.0 and `<= 6.0` would admit it — so 5.0 centres the gap: 2.0h of slack for
# venue kickoff skew (measured skew today: zero) and 1.0h below the tightest arrangement physics
# allows. The only doubleheader in our history sits at 8.5h [VERIFIED: MILSTL 2026-07-07, expiries
# captured from the venue], 3.5h clear.
#
# -1.0h is scheduling skew. Nothing legitimate is negative: a Kalshi market expiring BEFORE Poly's
# game starts is a different game — which is why the reverse mispairing (dh2 x G1, -2.5h) never
# leaked and every contaminated row is dh1 x G2.
_KALSHI_EXPIRY_MIN_H = -1.0
_KALSHI_EXPIRY_MAX_H = 5.0


def _within_kalshi_window(poly_start: str | None, km: KalshiMarket) -> bool:
    """True if this Kalshi market is plausibly the SAME GAME as the Poly pair starting at
    `poly_start`. This is a game-IDENTITY check wearing a time window, and the width IS the
    check — every hour of slack is another game it cannot tell apart.

    ⚠️ IT WAS 12h AND THAT SHIPPED CROSS-GAME HEDGES. The old bound came from a belief that
    expiry lands "3-9h after game start depending on sport"; measurement says 3.0h flat, so the
    extra 9 hours bought nothing and cost this: an MLB DOUBLEHEADER plays two games ~5.5h apart,
    so game 2's expiry sits 8.5h after game 1's START — inside +12h. Poly's `dh1` therefore
    matched Kalshi's `G2`, and nothing downstream compares game identity (the allowlist keys on
    SERIES, the sport gate sees MLB-vs-MLB, and 2.77% is far under the plausibility ceiling). The
    result is not an arb but a bet on `result(game1) >= result(game2)`, with a ~25% branch that
    loses the whole stake, booked as `guaranteed_profit`. It reached 149 of 264 would-fire rows
    [VERIFIED 2026-07-15 from would_fire.csv] and settlement scored it `clean` whenever the two
    games happened to agree. The reverse pairing (`dh2` x `G1`, -2.5h) was always rejected by the
    lower bound — which is exactly why every contaminated row is dh1+G2.

    Kalshi's close_time is a multi-day postponement buffer and cannot be used here.

    FAIL-CLOSED on anything unverifiable — a time we cannot check is not a match. This is the
    opposite of the old behaviour (three `return True` escapes) and matches the sport axis, which
    was made fail-closed after the 2026-06-23 Minnesota cross-sport incident. Safe: 0/72 live
    Kalshi markets lack the expiry field and 0/21 live Poly pairs lack a start [VERIFIED
    2026-07-15], so this drops nothing today; if a venue ever renames the field the cost is
    zero matches — loud and immediate — rather than a silently unbounded window.

    ⚠️ NBA/NHL are out of season and were NOT measured. The +3.0h offset is a Kalshi constant
    rather than a game length (a 2h WC match and a 3h MLB game both get it), so they will very
    likely match — but if their offset exceeds 5.0h they will match NOTHING when they return
    (~Oct), visible as zero pairs for the series rather than as a bad trade. Re-measure then; see
    docs/TODO.md.
    """
    if not poly_start:
        return False
    expiry = km.expected_expiration_time
    if not expiry:
        return False
    dt_poly = parse_iso(poly_start)
    dt_expiry = parse_iso(expiry)
    if dt_poly is None or dt_expiry is None:
        return False
    delta_hours = (dt_expiry - dt_poly).total_seconds() / 3600
    return _KALSHI_EXPIRY_MIN_H <= delta_hours <= _KALSHI_EXPIRY_MAX_H


def match_kalshi_events(
    poly_pairs: list[MarketPair],
    kalshi_markets: list[KalshiMarket],
    norm_table: dict[str, str],
) -> list[CrossPlatformPair]:
    """
    Pair Polymarket MarketPairs with Kalshi KalshiMarket objects.

    Each Poly pair matches at most one Kalshi market.
    Each Kalshi market is used at most once.
    """
    matched: list[CrossPlatformPair] = []
    used_kalshi: set[str] = set()

    for pair in poly_pairs:
        raw_a = _extract_team_name(pair.question_a)
        raw_b = _extract_team_name(pair.question_b)

        for km in kalshi_markets:
            if km.ticker in used_kalshi:
                continue
            # SPORT-consistency gate (before name matching): a Poly pair may only match a Kalshi
            # market of the SAME sport. Without this, shared city names cross sports — a Poly MLB
            # "Minnesota" (Twins) matched a Kalshi WNBA "Minnesota" (Lynx), a different game →
            # phantom edge. Fail-closed: an unmapped Poly sport (kalshi_series="") matches nothing.
            if pair.kalshi_series != km.ticker.split("-", 1)[0]:
                continue
            if not _within_kalshi_window(pair.start_date, km):
                continue

            yes_label = km.yes_side_label
            no_label  = km.no_side_label

            # Soccer / 3-outcome markets: Kalshi labels YES and NO with the same
            # team name (e.g. yes="Jordan", no="Jordan") because the market is
            # "Jordan wins YES / Jordan doesn't win NO". Match on a single team
            # and don't break — the same Poly pair can match multiple single-team
            # markets (one per team), generating separate CrossPlatformPairs.
            if yes_label == no_label:
                if _names_match(raw_a, yes_label, norm_table):
                    matched.append(CrossPlatformPair(
                        poly_pair=pair, kalshi_market=km,
                        poly_team_a=raw_a, poly_team_b=raw_b,
                        kalshi_yes_team=yes_label, kalshi_no_team=yes_label,
                    ))
                    used_kalshi.add(km.ticker)
                    log.debug(f"KalshiMatcher: '{pair.event_title}' ↔ '{km.ticker}' (single-team/soccer)")
                elif _names_match(raw_b, yes_label, norm_table):
                    matched.append(CrossPlatformPair(
                        poly_pair=pair, kalshi_market=km,
                        poly_team_a=raw_a, poly_team_b=raw_b,
                        kalshi_yes_team=yes_label, kalshi_no_team=yes_label,
                    ))
                    used_kalshi.add(km.ticker)
                    log.debug(f"KalshiMatcher: '{pair.event_title}' ↔ '{km.ticker}' (single-team/soccer)")
                continue  # keep scanning for more markets for this Poly pair

            # Standard two-team markets (NBA/NHL/MLB): match both sides.
            # Try: poly_a → kalshi YES, poly_b → kalshi NO
            if _names_match(raw_a, yes_label, norm_table) and _names_match(raw_b, no_label, norm_table):
                matched.append(CrossPlatformPair(
                    poly_pair=pair, kalshi_market=km,
                    poly_team_a=raw_a, poly_team_b=raw_b,
                    kalshi_yes_team=yes_label, kalshi_no_team=no_label,
                ))
                used_kalshi.add(km.ticker)
                log.debug(f"KalshiMatcher: '{pair.event_title}' ↔ '{km.ticker}'")
                break

            # Try: poly_a → kalshi NO, poly_b → kalshi YES
            if _names_match(raw_a, no_label, norm_table) and _names_match(raw_b, yes_label, norm_table):
                matched.append(CrossPlatformPair(
                    poly_pair=pair, kalshi_market=km,
                    poly_team_a=raw_a, poly_team_b=raw_b,
                    kalshi_yes_team=yes_label, kalshi_no_team=no_label,
                ))
                used_kalshi.add(km.ticker)
                log.debug(f"KalshiMatcher: '{pair.event_title}' ↔ '{km.ticker}' (swapped)")
                break

    log.info(
        f"KalshiMatcher: {len(matched)} crosspairs from {len(poly_pairs)} Poly games matched "
        f"against {len(kalshi_markets)} Kalshi markets"
    )
    return matched
