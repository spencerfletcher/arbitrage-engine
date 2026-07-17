"""Tests for match_kalshi_events — pairing Polymarket and Kalshi markets."""
import pytest
from bot.kalshi.scanner import KalshiMarket
from bot.core.types import MarketPair


def _pair(event_id="evt-1", q_a="Lakers wins", q_b="Celtics wins", start="2026-06-14T20:00:00Z",
          kalshi_series="KXNBAGAME"):
    return MarketPair(
        event_id=event_id, event_title="Lakers vs Celtics",
        token_yes_a="tok_a", token_no_a="tok_b", question_a=q_a,
        token_yes_b="tok_b", token_no_b="tok_a", question_b=q_b,
        start_date=start, kalshi_series=kalshi_series,
    )


def _kalshi(ticker="KXNBAGAME-LAKCEL-JUN14", yes_label="Lakers", no_label="Celtics",
            close_time="2026-06-18T23:59:00Z",  # multi-day postponement buffer (not used for matching)
            expected_expiration_time="2026-06-14T23:59:00Z"):
    return KalshiMarket(
        ticker=ticker, event_ticker="KXNBAGAME-LAKCEL",
        title="Lakers vs Celtics", subtitle="Jun 14",
        yes_side_label=yes_label, no_side_label=no_label,
        close_time=close_time, status="open",
        expected_expiration_time=expected_expiration_time,
    )


def test_exact_name_match():
    from bot.kalshi.matcher import match_kalshi_events
    pairs = match_kalshi_events([_pair()], [_kalshi()], norm_table={})
    assert len(pairs) == 1
    assert pairs[0].poly_team_a == "Lakers"
    assert pairs[0].poly_team_b == "Celtics"
    assert pairs[0].kalshi_yes_team == "Lakers"
    assert pairs[0].kalshi_no_team == "Celtics"


def _mlb_pair(q_a, q_b, kalshi_series="KXMLBGAME"):
    return MarketPair(
        event_id="mlb-1", event_title=f"{q_a} vs {q_b}",
        token_yes_a="slug", token_no_a="slug::short", question_a=f"{q_a} wins",
        token_yes_b="slug::short", token_no_b="slug", question_b=f"{q_b} wins",
        start_date="2026-06-18T17:35:00Z", kalshi_series=kalshi_series,
    )


def _mlb_kalshi(ticker, yes_label, no_label):
    return KalshiMarket(
        ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0],
        title="MLB game", subtitle="",
        yes_side_label=yes_label, no_side_label=no_label,
        close_time="2026-06-20T23:59:00Z", status="open",
        expected_expiration_time="2026-06-18T21:00:00Z",
    )


def test_mlb_full_name_matches_kalshi_short_label():
    """Poly full names ('Toronto Blue Jays') ↔ Kalshi short labels ('Toronto')."""
    from bot.kalshi.matcher import match_kalshi_events
    p = _mlb_pair("Toronto Blue Jays", "Boston Red Sox")
    k = _mlb_kalshi("KXMLBGAME-26JUN18TORBOS-TOR", "Toronto", "Boston")
    pairs = match_kalshi_events([p], [k], norm_table={})
    assert len(pairs) == 1
    assert pairs[0].kalshi_yes_team == "Toronto"


def test_mlb_shared_city_resolves_correct_team():
    """Cubs ('Chicago C') and White Sox ('Chicago WS') must resolve distinctly."""
    from bot.kalshi.matcher import match_kalshi_events
    p = _mlb_pair("Chicago Cubs", "Milwaukee Brewers")
    k = _mlb_kalshi("KXMLBGAME-26JUN18CHCMIL-CHC", "Chicago C", "Milwaukee")
    pairs = match_kalshi_events([p], [k], norm_table={})
    assert len(pairs) == 1
    assert pairs[0].kalshi_yes_team == "Chicago C"
    assert pairs[0].poly_team_a == "Chicago Cubs"
    # And the White Sox label must NOT match this Cubs market.
    from bot.core.matcher import _names_match
    assert _names_match("Chicago White Sox", "Chicago C", {}) is False
    assert _names_match("Chicago Cubs", "Chicago WS", {}) is False


def test_mlb_wrong_city_team_not_matched():
    """A Cubs (Chicago C) Kalshi market must not match a White Sox-only Poly pair."""
    from bot.kalshi.matcher import match_kalshi_events
    p = _mlb_pair("Chicago White Sox", "Detroit Tigers")
    k = _mlb_kalshi("KXMLBGAME-26JUN18CHCMIL-CHC", "Chicago C", "Milwaukee")
    assert match_kalshi_events([p], [k], norm_table={}) == []


def test_cross_sport_shared_city_not_matched():
    """REGRESSION (2026-06-23): a Poly MLB pair must NOT match a Kalshi WNBA market just because
    they share a city. 'Minnesota Twins' (MLB) matched 'Minnesota' (WNBA Lynx) → phantom 33.8%
    edge because the two legs settle on different games. The sport-consistency gate blocks it."""
    from bot.kalshi.matcher import match_kalshi_events
    # Poly MLB game (Dodgers @ Twins) — kalshi_series stamped KXMLBGAME by the scanner.
    p = _mlb_pair("Los Angeles Dodgers", "Minnesota Twins", kalshi_series="KXMLBGAME")
    # Kalshi WNBA single-team markets (Minnesota Lynx vs Washington Mystics), same night.
    wnba = _mlb_kalshi("KXWNBAGAME-26JUN24MINWSH-MIN", "Minnesota", "Minnesota")
    assert match_kalshi_events([p], [wnba], norm_table={}) == []


def test_no_cross_sport_pairs_in_mixed_pool():
    """STANDING INVARIANT — the gap that let the 2026-06-23 mispair through pre-wire verification:
    the WNBA check proved 'WNBA matches WNBA' but never 'WNBA doesn't wrongly match MLB on a shared
    city'. In a pool mixing series that share city names (Minnesota, Chicago in BOTH MLB + WNBA),
    EVERY crosspair must stay within its sport. Asserts the general property — a future matcher
    regression trips this, not just the one historical case. After any series add, this must hold."""
    from bot.kalshi.matcher import match_kalshi_events
    pairs = [
        _mlb_pair("Minnesota Twins", "Chicago Cubs", kalshi_series="KXMLBGAME"),
        MarketPair(  # WNBA pair sharing BOTH cities with the MLB pair above
            event_id="wnba-1", event_title="Minnesota Lynx vs Chicago Sky",
            token_yes_a="s", token_no_a="s::short", question_a="Minnesota Lynx wins",
            token_yes_b="s::short", token_no_b="s", question_b="Chicago Sky wins",
            start_date="2026-06-18T17:35:00Z", kalshi_series="KXWNBAGAME"),
    ]
    kms = [
        _mlb_kalshi("KXMLBGAME-26JUN18MINCHC-MIN", "Minnesota", "Minnesota"),
        _mlb_kalshi("KXMLBGAME-26JUN18MINCHC-CHC", "Chicago C", "Chicago C"),
        _mlb_kalshi("KXWNBAGAME-26JUN18MINCHI-MIN", "Minnesota", "Minnesota"),
        _mlb_kalshi("KXWNBAGAME-26JUN18MINCHI-CHI", "Chicago", "Chicago"),
    ]
    matched = match_kalshi_events(pairs, kms, norm_table={})
    assert len(matched) > 0  # same-sport matches DID form (gate isn't trivially passing on zero)
    # THE INVARIANT: no crosspair crosses sport — every Kalshi series == the Poly pair's sport.
    assert all(cp.kalshi_market.ticker.split("-", 1)[0] == cp.poly_pair.kalshi_series
               for cp in matched)


def test_same_sport_still_matches_after_gate():
    """The gate must not over-block: a Poly MLB pair still matches its KXMLBGAME market."""
    from bot.kalshi.matcher import match_kalshi_events
    p = _mlb_pair("Toronto Blue Jays", "Boston Red Sox", kalshi_series="KXMLBGAME")
    k = _mlb_kalshi("KXMLBGAME-26JUN18TORBOS-TOR", "Toronto", "Boston")
    assert len(match_kalshi_events([p], [k], norm_table={})) == 1


def test_unmapped_sport_fails_closed():
    """A pair with no kalshi_series ('' = unmapped sport) matches nothing — fail-closed."""
    from bot.kalshi.matcher import match_kalshi_events
    p = _pair(kalshi_series="")
    assert match_kalshi_events([p], [_kalshi()], norm_table={}) == []


def test_swapped_labels_still_match():
    """Kalshi may list Celtics as YES; matcher should still pair them."""
    from bot.kalshi.matcher import match_kalshi_events
    k = _kalshi(yes_label="Celtics", no_label="Lakers")
    pairs = match_kalshi_events([_pair()], [k], norm_table={})
    assert len(pairs) == 1
    assert pairs[0].kalshi_yes_team == "Celtics"
    assert pairs[0].kalshi_no_team == "Lakers"


def test_time_window_too_far_apart_rejected():
    """A Kalshi market expiring well after the game we're pricing is a different game."""
    from bot.kalshi.matcher import match_kalshi_events
    # Poly start 20:00Z, expiry next day 08:01Z → +12h 1min → rejected
    k = _kalshi(expected_expiration_time="2026-06-15T08:01:00Z")
    pairs = match_kalshi_events([_pair(start="2026-06-14T20:00:00Z")], [k], norm_table={})
    assert pairs == []


def test_time_window_accepts_the_MEASURED_offset():
    """+3.0h — the real value, not a hypothetical one.

    Kalshi's expected_expiration_time is start + 3.0h EXACTLY on every series we trade
    [VERIFIED 2026-07-15: 54/54 live MLB markets, 4/4 live matched WC pairs — one value, no
    spread]. The predecessor of this test asserted 7h30m "within 12h" — a number the venue never
    produces, pinning a belief rather than a measurement, and the 9 hours of slack that belief
    bought is precisely what let the doubleheader below through.
    """
    from bot.kalshi.matcher import match_kalshi_events
    k = _kalshi(expected_expiration_time="2026-06-14T23:00:00Z")   # start 20:00Z + 3.0h
    pairs = match_kalshi_events([_pair(start="2026-06-14T20:00:00Z")], [k], norm_table={})
    assert len(pairs) == 1


def test_mlb_doubleheader_game2_does_not_match_game1s_poly_pair():
    """THE cross-game bug, pinned with CAPTURED venue data — not a model of it.

    Both tickers and both `expected_expiration_time` values below were fetched from Kalshi
    (`GET /markets/{ticker}`, status=finalized) on 2026-07-16 — the real MILSTL 2026-07-07
    doubleheader that produced 149 of 264 would-fire rows. Game 1 starts 14:15 ET (18:15Z), game 2
    at 19:45 ET (23:45Z); expiry is start+3h, so game 2 expires **8.5h after game 1 STARTS**,
    which the old +12h window accepted.

    That pairing is not an arb: it is a bet on `result(g1) >= result(g2)`, with a branch that
    loses the entire stake, booked as guaranteed_profit. Nothing downstream can catch it — the
    allowlist keys on SERIES, the sport gate sees MLB-vs-MLB, and 2.77% is far under the
    plausibility ceiling. This gate is the only thing standing there.
    """
    from bot.kalshi.matcher import match_kalshi_events

    def _mlb(ticker, expiry):
        return KalshiMarket(
            ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0],
            title="Brewers vs Cardinals", subtitle="Jul 7",
            yes_side_label="Cardinals", no_side_label="Brewers",
            close_time="2026-07-10T23:59:00Z", status="open",
            expected_expiration_time=expiry,
        )

    # CAPTURED from the venue 2026-07-16, not derived:
    g1 = _mlb("KXMLBGAME-26JUL071415MILSTLG1-STL", "2026-07-07T21:15:00Z")   # +3.0h of 18:15Z
    g2 = _mlb("KXMLBGAME-26JUL071945MILSTLG2-STL", "2026-07-08T02:45:00Z")   # +3.0h of 23:45Z
    dh1 = _pair(start="2026-07-07T18:15:00Z", q_a="Cardinals wins", q_b="Brewers wins",
                kalshi_series="KXMLBGAME")

    # Game 1's Poly pair matches game 1 — and ONLY game 1.
    assert len(match_kalshi_events([dh1], [g1], norm_table={})) == 1
    assert match_kalshi_events([dh1], [g2], norm_table={}) == []
    # Offered both, it must take exactly the right one.
    got = match_kalshi_events([dh1], [g1, g2], norm_table={})
    assert [p.kalshi_market.ticker for p in got] == ["KXMLBGAME-26JUL071415MILSTLG1-STL"]


def test_unverifiable_time_FAILS_CLOSED():
    """A time we cannot check is not a match — the opposite of what this pinned before.

    All three escapes used to `return True`. The old test for the first one asserted that a pair
    with NO start matches a market expiring 2026-12-31 — six months out — and called it correct.
    That is the fail-open direction on the axis that decides WHICH GAME we hedge, on a money
    path, and it is the same class the sport axis was closed after the 2026-06-23 Minnesota
    cross-sport incident.

    Safe to close: 0/72 live Kalshi markets lack the expiry field and 0/21 live Poly pairs lack a
    start [VERIFIED 2026-07-15], so this drops nothing real. If a venue renames the field the
    cost is zero matches — loud and immediate — not a silently unbounded window.
    """
    from bot.kalshi.matcher import match_kalshi_events
    good_start, good_expiry = "2026-06-14T20:00:00Z", "2026-06-14T23:00:00Z"

    # Control: both sides present and 3.0h apart → matches.
    assert len(match_kalshi_events(
        [_pair(start=good_start)], [_kalshi(expected_expiration_time=good_expiry)],
        norm_table={})) == 1

    # No Poly start → cannot verify → no match (was: matched a market six months away).
    assert match_kalshi_events(
        [_pair(start=None)], [_kalshi(expected_expiration_time="2026-12-31T23:59:00Z")],
        norm_table={}) == []

    # No Kalshi expiry → cannot verify → no match.
    assert match_kalshi_events(
        [_pair(start=good_start)], [_kalshi(expected_expiration_time="")],
        norm_table={}) == []

    # Unparseable on either side → cannot verify → no match.
    assert match_kalshi_events(
        [_pair(start="not-a-date")], [_kalshi(expected_expiration_time=good_expiry)],
        norm_table={}) == []
    assert match_kalshi_events(
        [_pair(start=good_start)], [_kalshi(expected_expiration_time="not-a-date")],
        norm_table={}) == []


def test_no_match_returns_empty():
    from bot.kalshi.matcher import match_kalshi_events
    k = _kalshi(yes_label="Heat", no_label="Bucks")
    pairs = match_kalshi_events([_pair()], [k], norm_table={})
    assert pairs == []


def test_one_to_one_dedup():
    """Each Poly pair and each Kalshi market can appear in at most one CrossPlatformPair."""
    from bot.kalshi.matcher import match_kalshi_events
    k1 = _kalshi(ticker="KXNBAGAME-LAKCEL-JUN14")
    k2 = _kalshi(ticker="KXNBAGAME-LAKCEL-JUN15")
    pairs = match_kalshi_events([_pair()], [k1, k2], norm_table={})
    assert len(pairs) == 1  # first match wins


def test_expiry_window_bounds_sit_where_the_physics_puts_them():
    """The bound is not a taste call — it's forced, and this pins the reasoning.

    Kalshi's expiry is start + 3.0h flat (a Kalshi constant, not a game length: a ~2h WC match and
    a ~3h MLB game both get it). A doubleheader's game 2 cannot start before game 1 ends, so its
    expiry lands >6.0h after game 1's start. That leaves exactly one safe corridor:

        legit match  = 3.0h                      -> must ACCEPT
        wrong game   > 6.0h (physics, forced)    -> must REJECT

    Any bound in (3.0, 6.0) works; 6.0 itself does NOT, because a zero-turnaround doubleheader
    lands exactly on it and `<=` admits it. 5.0 centres the corridor.
    """
    from bot.kalshi.matcher import _KALSHI_EXPIRY_MAX_H, _KALSHI_EXPIRY_MIN_H
    assert 3.0 < _KALSHI_EXPIRY_MAX_H < 6.0, (
        "bound must sit strictly inside the corridor — 6.0 is the collision, not a margin")
    assert _KALSHI_EXPIRY_MAX_H - 3.0 >= 1.0, "leave >=1h for venue kickoff skew"
    assert _KALSHI_EXPIRY_MIN_H < 0, "a little negative skew is legitimate"
    assert _KALSHI_EXPIRY_MIN_H > -3.0, "but not enough to admit the reverse dh2 x G1 mispair"
