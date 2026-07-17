"""Tests for Kalshi arb math added to cross_arb.py."""
import math
import pytest
from unittest.mock import MagicMock

from bot.core import config


# ── kalshi_tick_floor ─────────────────────────────────────────────────────────

def test_kalshi_tick_floor_rounds_sub_cent_down():
    """Sub-cent prices (the bug that 400-rejected the Kalshi leg) floor to 1¢."""
    from bot.kalshi.cross_arb import kalshi_tick_floor
    assert kalshi_tick_floor(0.2850) == pytest.approx(0.28)
    assert kalshi_tick_floor(0.3047) == pytest.approx(0.30)
    assert kalshi_tick_floor(0.7150) == pytest.approx(0.71)


def test_kalshi_tick_floor_leaves_whole_cents_unchanged():
    """Float noise must not knock a whole cent down a tick (0.29 stays 0.29)."""
    from bot.kalshi.cross_arb import kalshi_tick_floor
    assert kalshi_tick_floor(0.29) == pytest.approx(0.29)
    assert kalshi_tick_floor(0.01) == pytest.approx(0.01)
    assert kalshi_tick_floor(0.70) == pytest.approx(0.70)


def test_kalshi_tick_floor_honors_a_non_default_tick():
    """A live non-1¢ tick floors to that grid (defensive; all markets are 1¢ today)."""
    from bot.kalshi.cross_arb import kalshi_tick_floor
    assert kalshi_tick_floor(0.2850, 0.05) == pytest.approx(0.25)   # nickel floor
    assert kalshi_tick_floor(0.27, 0.05) == pytest.approx(0.25)
    assert kalshi_tick_floor(0.30, 0.05) == pytest.approx(0.30)


def test_kalshi_tick_floor_default_tick_is_behavior_preserving():
    """Passing the 0.01 default is identical to the no-arg call at today's 1¢ tick — the swap
    changes nothing now. Compare call-to-call (exact), plus the literal cent value."""
    from bot.kalshi.cross_arb import kalshi_tick_floor
    for p in (0.2850, 0.3047, 0.7150, 0.29, 0.01):
        assert kalshi_tick_floor(p, 0.01) == kalshi_tick_floor(p)
    assert kalshi_tick_floor(0.2850, 0.01) == pytest.approx(0.28)


def test_kalshi_tick_floor_keeps_no_side_price_on_tick():
    """For a NO buy we send yes_price = 1 - floor(limit); both stay whole cents."""
    from bot.kalshi.cross_arb import kalshi_tick_floor
    limit = kalshi_tick_floor(0.2850)        # 0.28
    yes_price = round(1.0 - limit, 4)         # 0.72
    assert (yes_price * 100) % 1 == pytest.approx(0.0)


# ── _kalshi_taker_fee ─────────────────────────────────────────────────────────

# NOTE: these previously pinned a CENT ceiling (0.02 / 0.01) — that was the bug, copied from the
# fee schedule's DISPLAY table rather than its formula. Kalshi ceils to the CENTICENT. Corrected
# 2026-07-14 against real measured fills; see tests/test_kalshi_fee_model.py for the ground truth.

def test_kalshi_fee_at_50_cents():
    """At P=0.50: ceil(0.07 * 0.5 * 0.5 * 10000) / 10000 = 175/10000 = 0.0175.
    (The schedule's own 100-contract column confirms: $50.00 -> $1.75 = 0.0175/contract.)"""
    from bot.kalshi.cross_arb import _kalshi_taker_fee
    assert _kalshi_taker_fee(0.50) == pytest.approx(0.0175)


def test_kalshi_fee_at_30_cents():
    """At P=0.30: 0.07 * 0.3 * 0.7 = 0.0147 exactly (already a centicent).
    MEASURED on a real fill 2026-07-14: Kalshi charged $0.014700 — NOT the $0.02 we assumed."""
    from bot.kalshi.cross_arb import _kalshi_taker_fee
    assert _kalshi_taker_fee(0.30) == pytest.approx(0.0147)


def test_kalshi_fee_at_90_cents():
    """At P=0.90: 0.07 * 0.9 * 0.1 = 0.0063 exactly. (Old model said 0.01 — 59% too high.)"""
    from bot.kalshi.cross_arb import _kalshi_taker_fee
    assert _kalshi_taker_fee(0.90) == pytest.approx(0.0063)


def test_kalshi_fee_per_contract_is_now_size_independent():
    """10 @ 0.50: total = ceil(0.07*10*0.5*0.5*10000)/10000 = 0.175 -> 0.0175/contract.

    Property change worth pinning: with the CENT ceiling the per-contract fee varied with count
    (ceil waste amortised over the lot, 0.02 at n=1 vs 0.018 at n=10). At centicent precision the
    ceiling is negligible, so the per-contract fee is effectively size-independent."""
    from bot.kalshi.cross_arb import _kalshi_taker_fee
    assert _kalshi_taker_fee(0.50, count=10) == pytest.approx(0.0175)
    assert _kalshi_taker_fee(0.50, count=1) == pytest.approx(_kalshi_taker_fee(0.50, count=10))


# ── check_kalshi_arb ──────────────────────────────────────────────────────────

def test_check_kalshi_arb_positive_edge():
    """poly=0.45, kalshi=0.50: 0.45 + 0.50 + fee(0.0175) = 0.9675 → edge=0.0325.

    Was 0.03 under the old cent-ceiling fee — i.e. the bug UNDERSTATED this edge by 0.25c.
    That is the whole point of the 2026-07-14 fix: overstated fees rejected real edges."""
    from bot.kalshi.cross_arb import check_kalshi_arb
    edge = check_kalshi_arb(poly_ask=0.45, kalshi_ask=0.50, poly_fee_rate=0.0)
    assert edge == pytest.approx(0.0325)


def test_check_kalshi_arb_no_arb():
    """poly=0.55, kalshi=0.50: 0.55 + 0.50 + 0.02 = 1.07 → negative edge (no arb)."""
    from bot.kalshi.cross_arb import check_kalshi_arb
    edge = check_kalshi_arb(poly_ask=0.55, kalshi_ask=0.50, poly_fee_rate=0.0)
    assert edge < 0.0


def test_check_kalshi_arb_with_poly_fee():
    """With poly fee, effective cost is higher — harder to arb."""
    from bot.kalshi.cross_arb import check_kalshi_arb
    edge_no_fee = check_kalshi_arb(0.45, 0.50, poly_fee_rate=0.0)
    edge_with_fee = check_kalshi_arb(0.45, 0.50, poly_fee_rate=0.0175)
    assert edge_with_fee < edge_no_fee


def test_check_kalshi_arb_invalid_prices():
    from bot.kalshi.cross_arb import check_kalshi_arb
    assert check_kalshi_arb(0.0, 0.50, 0.0) < 0.0
    assert check_kalshi_arb(1.0, 0.50, 0.0) < 0.0
    assert check_kalshi_arb(0.45, 0.0, 0.0) < 0.0
    assert check_kalshi_arb(0.45, 1.0, 0.0) < 0.0


# ── find_kalshi_arbs ──────────────────────────────────────────────────────────

def _make_pair_and_feeds(
    poly_ask_a=0.45, poly_ask_b=0.58,
    kalshi_yes_ask=0.56, kalshi_no_ask=0.50,
    price_tick=0.01,
):
    """Build CrossPlatformPair + mock feeds returning specified prices."""
    from bot.kalshi.matcher import CrossPlatformPair
    from bot.kalshi.scanner import KalshiMarket
    from bot.core.types import MarketPair

    poly_pair = MarketPair(
        event_id="evt-1", event_title="Lakers vs Celtics",
        token_yes_a="tok_a", token_no_a="tok_b", question_a="Lakers wins",
        token_yes_b="tok_b", token_no_b="tok_a", question_b="Celtics wins",
        start_date="2026-06-14T20:00:00Z",
    )
    km = KalshiMarket(
        ticker="KXNBAGAME-LAKCEL-JUN14", event_ticker="KXNBAGAME-LAKCEL",
        title="Lakers vs Celtics", subtitle="Jun 14",
        yes_side_label="Lakers", no_side_label="Celtics",
        close_time="2026-06-14T23:59:00Z", status="open",
        price_tick=price_tick,
    )
    cp = CrossPlatformPair(
        poly_pair=poly_pair, kalshi_market=km,
        poly_team_a="Lakers", poly_team_b="Celtics",
        kalshi_yes_team="Lakers", kalshi_no_team="Celtics",
    )

    price_a = MagicMock()
    price_a.best_ask = poly_ask_a
    price_b = MagicMock()
    price_b.best_ask = poly_ask_b

    poly_feed = MagicMock()
    poly_feed.get_price = lambda tok: price_a if tok == "tok_a" else price_b

    kalshi_feed = MagicMock()
    kalshi_feed.get_best_ask = lambda ticker, side: (
        kalshi_yes_ask if side == "yes" else kalshi_no_ask
    )

    return cp, poly_feed, kalshi_feed


def test_find_kalshi_arbs_detects_direction_1(monkeypatch):
    """poly_ask_a=0.45 + kalshi_no=0.50 → edge=0.03 ≥ min_edge → one opportunity."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.005)
    from bot.kalshi.cross_arb import find_kalshi_arbs
    cp, poly_feed, kalshi_feed = _make_pair_and_feeds(
        poly_ask_a=0.45, kalshi_no_ask=0.50,
        kalshi_yes_ask=0.58, poly_ask_b=0.58,
    )
    opps = find_kalshi_arbs([cp], poly_feed, kalshi_feed)
    assert len(opps) == 1
    assert opps[0].poly_token == "tok_a"
    assert opps[0].kalshi_side == "no"
    assert opps[0].edge > 0
    assert opps[0].kalshi_tick == pytest.approx(0.01)   # default tick carried onto the opp


def test_find_kalshi_arbs_carries_market_price_tick_onto_opportunity(monkeypatch):
    """The wiring that matters: a non-default market price_tick reaches the opportunity, so the
    fire path floors the Kalshi limit to the market's real tick (kalshi_arb.py passes opp.kalshi_tick)."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.005)
    from bot.kalshi.cross_arb import find_kalshi_arbs
    cp, poly_feed, kalshi_feed = _make_pair_and_feeds(
        poly_ask_a=0.45, kalshi_no_ask=0.50, kalshi_yes_ask=0.58, poly_ask_b=0.58,
        price_tick=0.05,
    )
    opps = find_kalshi_arbs([cp], poly_feed, kalshi_feed)
    assert opps and opps[0].kalshi_tick == pytest.approx(0.05)


def test_find_kalshi_arbs_no_arb_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.005)
    from bot.kalshi.cross_arb import find_kalshi_arbs
    # Both directions: poly 0.55 + kalshi 0.50 → no arb
    cp, poly_feed, kalshi_feed = _make_pair_and_feeds(
        poly_ask_a=0.55, poly_ask_b=0.55,
        kalshi_yes_ask=0.50, kalshi_no_ask=0.50,
    )
    opps = find_kalshi_arbs([cp], poly_feed, kalshi_feed)
    assert opps == []


def test_find_kalshi_arbs_returns_all_distinct_directions(monkeypatch):
    """Both crossing directions on one event are returned — each is a distinct hedged
    position that fires on its own (was: one-best-per-event), sorted by edge desc."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.0)
    from bot.kalshi.cross_arb import find_kalshi_arbs
    cp, poly_feed, kalshi_feed = _make_pair_and_feeds(
        poly_ask_a=0.44, poly_ask_b=0.43,
        kalshi_yes_ask=0.49, kalshi_no_ask=0.49,
    )
    opps = find_kalshi_arbs([cp], poly_feed, kalshi_feed)
    assert len(opps) == 2
    # two distinct positions (different poly token + kalshi side)
    assert len({(o.poly_token, o.kalshi_ticker, o.kalshi_side) for o in opps}) == 2
    assert opps[0].edge >= opps[1].edge              # sorted by edge descending


def test_find_kalshi_arbs_dominated_out_empty_for_distinct_directions(monkeypatch):
    """dominated_out now collects ONLY exact-duplicate positions; distinct directions on
    one event are all returned, nothing dominated."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.0)
    from bot.kalshi.cross_arb import find_kalshi_arbs
    cp, poly_feed, kalshi_feed = _make_pair_and_feeds(
        poly_ask_a=0.44, poly_ask_b=0.43,
        kalshi_yes_ask=0.49, kalshi_no_ask=0.49,
    )
    dominated = []
    opps = find_kalshi_arbs([cp], poly_feed, kalshi_feed, dominated_out=dominated)
    assert len(opps) == 2
    assert dominated == []


# ── Cross-pair directions (Task 8) ───────────────────────────────────────────

def _make_single_team_pair(
    event_id: str,
    poly_token_a: str,
    poly_token_b: str,
    kalshi_team: str,
    is_team_a: bool,
    kalshi_ticker: str,
    event_ticker_prefix: str = "KXMLBGAME",
):
    """Build a single-team CrossPlatformPair (MLB/NBA/NHL format)."""
    from bot.kalshi.matcher import CrossPlatformPair
    from bot.kalshi.scanner import KalshiMarket
    from bot.core.types import MarketPair

    poly_pair = MarketPair(
        event_id=event_id, event_title="Pittsburgh vs A's",
        token_yes_a=poly_token_a, token_no_a="dummy",
        question_a="Pittsburgh wins",
        token_yes_b=poly_token_b, token_no_b="dummy",
        question_b="A's win",
        start_date="2026-07-07T23:05:00Z",
    )
    km = KalshiMarket(
        ticker=kalshi_ticker,
        event_ticker=f"{event_ticker_prefix}-PITATH",
        title="Pittsburgh vs A's", subtitle="Jul 7",
        yes_side_label=kalshi_team, no_side_label=kalshi_team,
        close_time="2026-07-08T03:05:00Z", status="active",
    )
    team_a, team_b = "Pittsburgh", "A's"
    return CrossPlatformPair(
        poly_pair=poly_pair, kalshi_market=km,
        poly_team_a=team_a, poly_team_b=team_b,
        kalshi_yes_team=kalshi_team, kalshi_no_team=kalshi_team,
    )


def _make_cross_feeds(ask_pit: float, ask_ath: float, kalshi_pit_yes: float, kalshi_ath_yes: float):
    """Return (poly_feed, kalshi_feed) mocks with the given prices."""
    poly_feed = MagicMock()
    price_pit = MagicMock(); price_pit.best_ask = ask_pit
    price_ath = MagicMock(); price_ath.best_ask = ask_ath
    poly_feed.get_price = lambda tok: price_pit if tok == "tok_pit" else price_ath

    kalshi_feed = MagicMock()
    def get_best_ask(ticker, side):
        if ticker == "KXMLBGAME-PIT":
            return (1 - kalshi_pit_yes) if side == "no" else kalshi_pit_yes
        if ticker == "KXMLBGAME-ATH":
            return (1 - kalshi_ath_yes) if side == "no" else kalshi_ath_yes
        return None
    kalshi_feed.get_best_ask = get_best_ask
    return poly_feed, kalshi_feed


def test_cross_pair_direction_detected(monkeypatch):
    """Cross-dir: Poly Pittsburgh YES + Kalshi A's YES should be detected for MLB."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.0)
    from bot.kalshi.cross_arb import find_kalshi_arbs

    pair_pit = _make_single_team_pair("evt-1", "tok_pit", "tok_ath", "Pittsburgh", True, "KXMLBGAME-PIT")
    pair_ath = _make_single_team_pair("evt-1", "tok_pit", "tok_ath", "A's", False, "KXMLBGAME-ATH")
    # Pittsburgh: Poly=0.46, Kalshi YES=0.52, Kalshi NO=0.48
    # A's:        Poly=0.56, Kalshi YES=0.44, Kalshi NO=0.56
    # Cross-dir3: Poly Pittsburgh(0.46) + Kalshi A's YES(0.44) = 0.90 + fee → big edge
    poly_feed, kalshi_feed = _make_cross_feeds(0.46, 0.56, 0.52, 0.44)
    opps = find_kalshi_arbs([pair_pit, pair_ath], poly_feed, kalshi_feed)
    # The cross direction (Poly Pittsburgh + Kalshi A's YES) is among those returned.
    match = [o for o in opps if o.kalshi_ticker == "KXMLBGAME-ATH"
             and o.kalshi_side == "yes" and o.poly_token == "tok_pit"]
    assert len(match) == 1


def test_cross_pair_within_pair_sorts_above_cross_pair(monkeypatch):
    """Both directions now return; the higher-edge within-pair one sorts first, and the
    weaker cross-pair direction is still present (no longer discarded)."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.0)
    from bot.kalshi.cross_arb import find_kalshi_arbs

    pair_pit = _make_single_team_pair("evt-2", "tok_pit", "tok_ath", "Pittsburgh", True, "KXMLBGAME-PIT")
    pair_ath = _make_single_team_pair("evt-2", "tok_pit", "tok_ath", "A's", False, "KXMLBGAME-ATH")
    # Within-dir: Poly Pit(0.44) + Kalshi Pit NO(0.48) = 0.92 → edge ~0.06
    # Cross-dir3: Poly Pit(0.44) + Kalshi A's YES(0.50) = 0.94 → edge ~0.04
    poly_feed, kalshi_feed = _make_cross_feeds(0.44, 0.60, 0.52, 0.50)
    opps = find_kalshi_arbs([pair_pit, pair_ath], poly_feed, kalshi_feed)
    assert opps[0].kalshi_ticker == "KXMLBGAME-PIT"   # highest edge sorts first
    assert opps[0].kalshi_side == "no"
    # the weaker cross-pair direction is still captured, not dropped
    assert any(o.kalshi_ticker == "KXMLBGAME-ATH" and o.kalshi_side == "yes" for o in opps)


def _mlb_pairs(slug="aec-mlb-tor-bos"):
    """Real Poly US MLB shape, built via the actual matcher: a moneyline MarketPair
    (long→token_yes_a, short→'<slug>::short') matched against the TWO single-team
    Kalshi markets per game (yes==no). Returns the list of CrossPlatformPairs."""
    from bot.kalshi.matcher import match_kalshi_events
    from bot.kalshi.scanner import KalshiMarket
    from bot.core.types import MarketPair
    from bot.poly_us.sides import short_token

    poly_pair = MarketPair(
        event_id="mlb-tor-bos", event_title="Toronto Blue Jays vs. Boston Red Sox",
        token_yes_a=slug, token_no_a=short_token(slug), question_a="Toronto Blue Jays wins",
        token_yes_b=short_token(slug), token_no_b=slug, question_b="Boston Red Sox wins",
        start_date="2026-06-18T17:35:00Z", taker_fee_rate=0.05,
        settlement_type="moneyline", kalshi_series="KXMLBGAME",
    )
    def _km(suffix, team):
        return KalshiMarket(
            ticker=f"KXMLBGAME-26JUN18TORBOS-{suffix}", event_ticker="KXMLBGAME-26JUN18TORBOS",
            title="Toronto vs Boston", subtitle="",
            yes_side_label=team, no_side_label=team,  # single-team: yes == no
            close_time="2026-06-20T23:59:00Z", status="open",
            expected_expiration_time="2026-06-18T21:00:00Z",
        )
    return match_kalshi_events([poly_pair], [_km("TOR", "Toronto"), _km("BOS", "Boston")], {})


def _us_feed_with_book(slug, ask, ask_qty, bid, bid_qty):
    """Real PolyUSOrderBookCache primed via a market_data book, wrapped in the adapter."""
    from bot.poly_us.feed import PolyUSOrderBookCache
    from bot.runner.common import _PolyUSFeedAdapter
    cache = PolyUSOrderBookCache(sdk=None)
    cache._on_market_data({"marketData": {
        "marketSlug": slug,
        "offers": [{"px": {"value": f"{ask}", "currency": "USD"}, "qty": str(ask_qty)}],
        "bids":   [{"px": {"value": f"{bid}", "currency": "USD"}, "qty": str(bid_qty)}],
    }})
    return _PolyUSFeedAdapter(cache)


def _mlb_kalshi_feed(tor_yes, tor_no, bos_yes, bos_no):
    """Mock Kalshi feed for the two single-team MLB markets (-TOR, -BOS)."""
    feed = MagicMock()
    def get_best_ask(ticker, side):
        if ticker.endswith("-TOR"):
            return tor_yes if side == "yes" else tor_no
        if ticker.endswith("-BOS"):
            return bos_yes if side == "yes" else bos_no
        return None
    feed.get_best_ask = get_best_ask
    return feed


def test_mlb_long_side_arb_routes_to_bare_slug(monkeypatch):
    """Long team (Toronto) cheap on Poly → fires via the -TOR market, poly_token =
    bare slug (Poly Toronto-YES + Kalshi Toronto-NO)."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.0)
    from bot.kalshi.cross_arb import find_kalshi_arbs
    pairs = _mlb_pairs()
    assert len(pairs) == 2  # two single-team pairs per game (real shape)
    # long ask 0.40 (Toronto cheap); short ask = 1−0.38 = 0.62 (Boston pricey).
    poly_feed = _us_feed_with_book("aec-mlb-tor-bos", 0.40, 50, 0.38, 50)
    kalshi_feed = _mlb_kalshi_feed(tor_yes=0.62, tor_no=0.40, bos_yes=0.62, bos_no=0.40)
    opps = find_kalshi_arbs(pairs, poly_feed, kalshi_feed)
    assert len(opps) == 1                              # deduped to one per event
    assert opps[0].poly_token == "aec-mlb-tor-bos"     # long → bare slug
    assert opps[0].kalshi_ticker.endswith("-TOR")


def test_mlb_short_side_arb_routes_to_short_token(monkeypatch):
    """Short team (Boston) cheap on Poly → fires via the -BOS market, poly_token =
    '<slug>::short' (Poly Boston-YES + Kalshi Boston-NO)."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.0)
    from bot.kalshi.cross_arb import find_kalshi_arbs
    pairs = _mlb_pairs()
    # long ask 0.62 (Toronto pricey); short ask = 1−0.60 = 0.40 (Boston cheap).
    poly_feed = _us_feed_with_book("aec-mlb-tor-bos", 0.62, 50, 0.60, 50)
    kalshi_feed = _mlb_kalshi_feed(tor_yes=0.62, tor_no=0.40, bos_yes=0.62, bos_no=0.40)
    opps = find_kalshi_arbs(pairs, poly_feed, kalshi_feed)
    assert len(opps) == 1
    assert opps[0].poly_token == "aec-mlb-tor-bos::short"  # short → suffixed token
    assert opps[0].kalshi_ticker.endswith("-BOS")


def test_cross_pair_skipped_for_soccer(monkeypatch):
    """Soccer (KXWCGAME) must NOT use cross-pair directions due to draw risk."""
    monkeypatch.setattr(config, "KALSHI_ARB_MIN_EDGE", 0.0)
    from bot.kalshi.cross_arb import find_kalshi_arbs

    pair_jor = _make_single_team_pair(
        "evt-3", "tok_jor", "tok_arg", "Jordan", True,
        "KXWCGAME-JOR", event_ticker_prefix="KXWCGAME",
    )
    pair_arg = _make_single_team_pair(
        "evt-3", "tok_jor", "tok_arg", "Argentina", False,
        "KXWCGAME-ARG", event_ticker_prefix="KXWCGAME",
    )
    # Prices that look like a big cross-dir arb (0.40 + 0.44 = 0.84) but invalid for soccer
    poly_feed, kalshi_feed = _make_cross_feeds(0.40, 0.55, 0.58, 0.44)
    opps = find_kalshi_arbs([pair_jor, pair_arg], poly_feed, kalshi_feed)
    for opp in opps:
        assert not (opp.poly_token == "tok_jor" and opp.kalshi_ticker == "KXWCGAME-ARG"), \
            "Cross-pair soccer arb must not be generated"


# ── find_macro_arbs ───────────────────────────────────────────────────────────

def _macro_pair(kalshi_side="no"):
    from bot.kalshi.macro_pairs import MacroPair
    return MacroPair(
        poly_condition_id="0xabc",
        poly_token="tok_yes",
        kalshi_ticker="KXFED-26JUN-C25",
        kalshi_side=kalshi_side,
        comment="complementary by hand",
        category="fed",
    )


def _poly_feed_macro(best_ask):
    feed = MagicMock()
    if best_ask is None:
        feed.get_price.return_value = None
    else:
        price = MagicMock()
        price.best_ask = best_ask
        feed.get_price.return_value = price
    return feed


def _kalshi_feed_macro(best_ask):
    feed = MagicMock()
    feed.get_best_ask.return_value = best_ask
    return feed


def test_find_macro_arbs_positive_edge_no_side():
    """poly=0.45 (fee 0.04), kalshi NO=0.50 (fee 0.02): edge should be positive above 0.01."""
    from bot.kalshi.cross_arb import find_macro_arbs
    opps = find_macro_arbs([_macro_pair("no")], _poly_feed_macro(0.45), _kalshi_feed_macro(0.50),
                           min_edge=0.01)
    assert len(opps) == 1
    opp = opps[0]
    assert opp.kalshi_ticker == "KXFED-26JUN-C25"
    assert opp.kalshi_side == "no"
    assert opp.poly_token == "tok_yes"
    assert opp.poly_event_id == "0xabc"
    assert opp.edge > 0.01
    assert opp.shares >= 1
    assert opp.guaranteed_profit == pytest.approx(opp.shares * 1.0 - opp.total_cost)


def test_find_macro_arbs_reads_requested_kalshi_side():
    """kalshi_side='yes' must drive the get_best_ask side argument."""
    from bot.kalshi.cross_arb import find_macro_arbs
    kf = _kalshi_feed_macro(0.50)
    find_macro_arbs([_macro_pair("yes")], _poly_feed_macro(0.45), kf, min_edge=0.01)
    kf.get_best_ask.assert_called_with("KXFED-26JUN-C25", "yes")


def test_find_macro_arbs_below_threshold_returns_nothing():
    from bot.kalshi.cross_arb import find_macro_arbs
    # poly=0.49, kalshi=0.50 → edge below 0.01
    opps = find_macro_arbs([_macro_pair("no")], _poly_feed_macro(0.49), _kalshi_feed_macro(0.50),
                           min_edge=0.01)
    assert opps == []


def test_find_macro_arbs_missing_poly_quote_skips():
    from bot.kalshi.cross_arb import find_macro_arbs
    opps = find_macro_arbs([_macro_pair("no")], _poly_feed_macro(None), _kalshi_feed_macro(0.50),
                           min_edge=0.01)
    assert opps == []


def test_find_macro_arbs_missing_kalshi_quote_skips():
    from bot.kalshi.cross_arb import find_macro_arbs
    opps = find_macro_arbs([_macro_pair("no")], _poly_feed_macro(0.45), _kalshi_feed_macro(None),
                           min_edge=0.01)
    assert opps == []


# ── depth-aware sizing ────────────────────────────────────────────────────────

def test_size_caps_shares_at_poly_depth(monkeypatch):
    """shares is capped at available Poly depth (Kalshi MM fills any size)."""
    from bot.kalshi.cross_arb import _size_kalshi_opportunity
    monkeypatch.setattr(config, "MAX_POSITION_USD", 500)
    pair = _make_single_team_pair("evt-1", "tok_pit", "tok_ath", "Pittsburgh", True, "KXMLBGAME-PIT")
    opp = _size_kalshi_opportunity(pair, "a", "no", 0.10, 0.10, 0.0, 0.80, poly_ask_depth=5)
    assert opp.shares == 5


def test_size_uncapped_when_depth_unknown(monkeypatch):
    """With depth unknown (0), fall back to budget-based sizing (no cap)."""
    from bot.kalshi.cross_arb import _size_kalshi_opportunity
    monkeypatch.setattr(config, "MAX_POSITION_USD", 500)
    pair = _make_single_team_pair("evt-1", "tok_pit", "tok_ath", "Pittsburgh", True, "KXMLBGAME-PIT")
    opp = _size_kalshi_opportunity(pair, "a", "no", 0.10, 0.10, 0.0, 0.80, poly_ask_depth=0)
    assert opp.shares > 5


def test_size_caps_shares_at_poly_balance(monkeypatch):
    """shares capped so the Poly leg cost stays within available Poly funds."""
    from bot.kalshi.cross_arb import _size_kalshi_opportunity
    monkeypatch.setattr(config, "MAX_POSITION_USD", 500)
    pair = _make_single_team_pair("evt-1", "tok_pit", "tok_ath", "Pittsburgh", True, "KXMLBGAME-PIT")
    # poly_effective ~0.10; $10 balance * 0.95 / 0.10 = 95 shares max
    opp = _size_kalshi_opportunity(pair, "a", "no", 0.10, 0.10, 0.0, 0.80, poly_balance=10.0)
    assert opp.shares == 95


def test_size_balance_cap_skipped_when_none(monkeypatch):
    """No balance cap when balance is None (e.g. logging/tightest calls)."""
    from bot.kalshi.cross_arb import _size_kalshi_opportunity
    monkeypatch.setattr(config, "MAX_POSITION_USD", 500)
    pair = _make_single_team_pair("evt-1", "tok_pit", "tok_ath", "Pittsburgh", True, "KXMLBGAME-PIT")
    opp = _size_kalshi_opportunity(pair, "a", "no", 0.10, 0.10, 0.0, 0.80, poly_balance=None)
    assert opp.shares > 95
