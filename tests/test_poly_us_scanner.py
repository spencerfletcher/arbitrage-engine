"""Tests for PolyUSScanner — event JSON → MarketPair."""
import pytest

from bot.poly_us.scanner import PolyUSScanner
from bot.core.types import MarketPair


def _event():
    """Minimal Poly US event with two team Yes-sides (binary-style)."""
    return {
        "slug": "fwc-fra-sen-2026-06-16",
        "title": "France vs. Senegal",
        "startTime": "2026-06-16T19:00:00Z",
        "active": True,
        "closed": False,
        "markets": [
            {
                "slug": "atc-fwc-fra-sen-2026-06-16-fra",
                "sportsMarketType": "drawable_outcome",
                "marketSides": [
                    {"identifier": "atc-fwc-fra-sen-2026-06-16-fra",
                     "description": "Yes", "long": True,
                     "team": {"name": "France"}},
                ],
            },
            {
                "slug": "atc-fwc-fra-sen-2026-06-16-sen",
                "sportsMarketType": "drawable_outcome",
                "marketSides": [
                    {"identifier": "atc-fwc-fra-sen-2026-06-16-sen",
                     "description": "Yes", "long": True,
                     "team": {"name": "Senegal"}},
                ],
            },
        ],
    }


class _FakeEvents:
    def __init__(self, events):
        self._events = events
        self.calls = []
    async def list(self, params=None):
        self.calls.append(params)
        # Honor limit/offset so pagination is exercised like the real API.
        params = params or {}
        offset = params.get("offset", 0)
        limit = params.get("limit", len(self._events))
        return {"events": self._events[offset:offset + limit]}


class _FakeSDK:
    def __init__(self, events):
        self.events = _FakeEvents(events)


@pytest.mark.asyncio
async def test_fetch_markets_builds_pair_from_team_yes_sides():
    scanner = PolyUSScanner(_FakeSDK([_event()]))
    pairs = await scanner.fetch_markets(["69"])
    assert len(pairs) == 1
    p = pairs[0]
    assert isinstance(p, MarketPair)
    assert p.event_title == "France vs. Senegal"
    assert p.start_date == "2026-06-16T19:00:00Z"
    assert p.token_yes_a == "atc-fwc-fra-sen-2026-06-16-fra"
    assert p.token_yes_b == "atc-fwc-fra-sen-2026-06-16-sen"
    assert "France" in p.question_a
    assert "Senegal" in p.question_b
    # Poly US taker fee must be modeled (Θ=0.05) so edges are net, not gross.
    # (there is no fees_enabled flag any more — it defaulted False and silently zeroed the
    #  fee if a construction site forgot it. taker_fee_rate is read directly and is
    #  self-fail-safe. See tests/test_fee_flag_removed.py.)
    assert p.taker_fee_rate > 0, "the pair must carry a non-zero fee"
    # no feeCoefficient on this fixture → the FALLBACK is used. Assert the WIRING, not the
    # literal: the fallback's VALUE is pinned to real exchange commissions in
    # tests/test_poly_fee_model.py. (This asserted 0.05 until Poly raised Θ to 0.06.)
    assert p.taker_fee_rate == pytest.approx(_POLY_US_TAKER_THETA)


@pytest.mark.asyncio
async def test_fetch_markets_skips_non_drawable_knockout_market():
    """Knockout / w/o-tie markets resolve on extra time/penalties → the cross-venue
    hedge isn't settlement-safe, so they must not produce a pair."""
    ev = _event()
    for m in ev["markets"]:
        m["sportsMarketType"] = "single_winner"  # not drawable_outcome
    pairs = await PolyUSScanner(_FakeSDK([ev])).fetch_markets(["69"])
    assert pairs == []


@pytest.mark.asyncio
async def test_fetch_markets_passes_seriesid_and_active_filter():
    sdk = _FakeSDK([_event()])
    await PolyUSScanner(sdk).fetch_markets(["69"])
    assert sdk.events.calls[0]["seriesId"] == "69"
    assert sdk.events.calls[0]["active"] is True


@pytest.mark.asyncio
async def test_fetch_markets_skips_closed_event():
    ev = _event()
    ev["closed"] = True
    pairs = await PolyUSScanner(_FakeSDK([ev])).fetch_markets(["69"])
    assert pairs == []


# ── taker fee read live from feeCoefficient, with a fail-safe-direction fallback ──────────────
# (The existing builds-pair test above asserts 0.05 on an event with NO feeCoefficient, so it
# already pins the absent→fallback case; these add present→live, the helper direction, and the
# behavior-preserving equality through the cost formula.)
from bot.poly_us.scanner import _coerce_fee_rate, _event_fee_rate, _POLY_US_TAKER_THETA
from bot.kalshi.cross_arb import _effective_share_cost


@pytest.mark.asyncio
async def test_fee_rate_read_live_from_feecoefficient():
    ev = _event()
    for m in ev["markets"]:
        m["feeCoefficient"] = 0.07          # venue reports a non-default coefficient
    p = (await PolyUSScanner(_FakeSDK([ev])).fetch_markets(["69"]))[0]
    assert p.taker_fee_rate == pytest.approx(0.07)   # live value used, not the 0.05 default


def test_coerce_fee_rate_falls_back_to_known_fee_never_zero():
    # Fail-safe DIRECTION: missing/null/non-numeric/non-positive → the known fee, NEVER 0
    # (a zero fee would under-cost the edge and over-size). Present positive → used verbatim.
    assert _coerce_fee_rate(0.07) == pytest.approx(0.07)
    for bad in (None, 0, 0.0, -0.01, "", "abc"):
        assert _coerce_fee_rate(bad) == _POLY_US_TAKER_THETA   # == 0.05, never 0/None
    # _event_fee_rate with no matching market type also falls back, never crashes.
    assert _event_fee_rate({"markets": []}, "drawable_outcome") == _POLY_US_TAKER_THETA


def test_a_live_feeCoefficient_beats_the_fallback():
    """The live per-market value must WIN over the hardcoded fallback. That is the entire point of
    reading feeCoefficient, and it is what saved us: Poly RAISED Θ 0.05 → 0.06 between 2026-06-17
    and 2026-07-15 [VERIFIED against real exchange commissions, tests/test_poly_fee_model.py] and
    the live path self-corrected with no code change.

    This test previously asserted "live (0.05) and the hardcoded Θ produce the IDENTICAL cost — the
    swap changes nothing today". That premise expired the moment the venue moved. A test whose only
    content is "two constants happen to be equal today" cannot notice reality changing; it just
    stays green. Assert the RELATIONSHIP (live overrides) and pin the VALUE against real fills.
    """
    p = 0.85
    assert _effective_share_cost(p, 0.06) == pytest.approx(0.85 + 0.06 * 0.85 * 0.15)
    # a market reporting something OTHER than the fallback must be costed at ITS own value
    assert _effective_share_cost(p, 0.07) != _effective_share_cost(p, _POLY_US_TAKER_THETA)


@pytest.mark.asyncio
async def test_fetch_markets_skips_event_with_one_team():
    ev = _event()
    ev["markets"] = ev["markets"][:1]  # only one team side
    pairs = await PolyUSScanner(_FakeSDK([ev])).fetch_markets(["69"])
    assert pairs == []


def _mlb_event():
    """MLB moneyline event: ONE binary market, two sides (long team / short team)."""
    return {
        "slug": "mlb-tor-bos-2026-06-18",
        "title": "Toronto Blue Jays vs. Boston Red Sox",
        "startTime": "2026-06-18T17:35:00Z",
        "active": True,
        "closed": False,
        "markets": [
            {
                "slug": "aec-mlb-tor-bos-2026-06-18",
                "sportsMarketType": "moneyline",
                "marketSides": [
                    {"identifier": "aec-mlb-tor-bos-2026-06-18",
                     "description": "Toronto Blue Jays", "long": True,
                     "team": {"name": "Toronto Blue Jays"}},
                    {"identifier": "aec-mlb-tor-bos-2026-06-18",
                     "description": "Boston Red Sox", "long": False,
                     "team": {"name": "Boston Red Sox"}},
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_fetch_markets_builds_moneyline_pair_both_sides():
    """MLB moneyline: long side → token_yes_a (bare slug), short side → token_yes_b
    (slug::short). One game = one slug, both teams tradeable."""
    pairs = await PolyUSScanner(_FakeSDK([_mlb_event()])).fetch_markets(["15"])
    assert len(pairs) == 1
    p = pairs[0]
    assert p.settlement_type == "moneyline"
    assert p.token_yes_a == "aec-mlb-tor-bos-2026-06-18"          # long = Toronto
    assert p.token_yes_b == "aec-mlb-tor-bos-2026-06-18::short"   # short = Boston
    assert "Toronto Blue Jays" in p.question_a
    assert "Boston Red Sox" in p.question_b
    # (there is no fees_enabled flag any more — it defaulted False and silently zeroed the
    #  fee if a construction site forgot it. taker_fee_rate is read directly and is
    #  self-fail-safe. See tests/test_fee_flag_removed.py.)
    assert p.taker_fee_rate > 0, "the pair must carry a non-zero fee"
    # no feeCoefficient on this fixture → the FALLBACK is used. Assert the WIRING, not the
    # literal: the fallback's VALUE is pinned to real exchange commissions in
    # tests/test_poly_fee_model.py. (This asserted 0.05 until Poly raised Θ to 0.06.)
    assert p.taker_fee_rate == pytest.approx(_POLY_US_TAKER_THETA)


@pytest.mark.asyncio
async def test_fetch_markets_builds_moneyline_pair_after_poly_type_rename():
    """Regression: Poly renamed MLB's winner type 'moneyline' → 'baseball_team_full_game_winner'
    (~2026-06-23), which silently stopped ALL MLB matching. The new type must still build a pair,
    with the economic 'moneyline' settlement label unchanged (the settlement-equivalence allowlist
    key)."""
    ev = _mlb_event()
    ev["markets"][0]["sportsMarketType"] = "baseball_team_full_game_winner"
    ev["markets"][0]["feeCoefficient"] = 0.05
    pairs = await PolyUSScanner(_FakeSDK([ev])).fetch_markets(["15"])
    assert len(pairs) == 1
    p = pairs[0]
    assert p.settlement_type == "moneyline"                       # label for the settlement gate
    assert p.token_yes_a == "aec-mlb-tor-bos-2026-06-18"
    assert p.token_yes_b == "aec-mlb-tor-bos-2026-06-18::short"
    assert p.taker_fee_rate == pytest.approx(0.05)                # fee read from the renamed market


@pytest.mark.asyncio
async def test_pair_stamped_with_kalshi_series_for_sport():
    """The sport-consistency field: each pair carries the Kalshi series for its Poly sport, so the
    matcher can't cross sports (a Poly MLB 'Minnesota' must not match a Kalshi WNBA 'Minnesota')."""
    mlb = await PolyUSScanner(_FakeSDK([_mlb_event()])).fetch_markets(["15"])
    assert mlb[0].kalshi_series == "KXMLBGAME"                     # seriesId 15 → KXMLBGAME
    wnba_ev = _mlb_event()
    wnba_ev["markets"][0]["sportsMarketType"] = "basketball_team_full_game_winner"
    wnba = await PolyUSScanner(_FakeSDK([wnba_ev])).fetch_markets(["49"])
    assert wnba[0].kalshi_series == "KXWNBAGAME"                   # seriesId 49 → KXWNBAGAME
    unk = await PolyUSScanner(_FakeSDK([_mlb_event()])).fetch_markets(["999"])
    assert unk[0].kalshi_series == ""                             # unmapped sid → fail-closed


def test_is_moneyline_market_type_accepts_winner_rename_rejects_non_equivalent():
    from bot.poly_us.scanner import _is_moneyline_market_type
    assert _is_moneyline_market_type("moneyline")                       # legacy
    assert _is_moneyline_market_type("baseball_team_full_game_winner")  # the 2026-06-23 rename
    assert _is_moneyline_market_type("basketball_team_full_game_winner")  # future-proof NBA/NHL
    # NOT settlement-equivalent to a full-game winner → must be rejected:
    for bad in ("baseball_team_first_five_winner", "baseball_team_full_game_spread",
                "baseball_team_full_game_total", "futures", "drawable_outcome", None, 123):
        assert not _is_moneyline_market_type(bad)


@pytest.mark.asyncio
async def test_fetch_markets_builds_wc_pair_after_soccer_type_rename():
    """Regression (G3): Poly renamed the WC winner type 'drawable_outcome' →
    'soccer_team_full_time_winner' (~2026-06-24), which silently dropped ALL World Cup matching
    (0 cross-pairs → the KXWCGAME schema-drift alert). The renamed type must still build a pair,
    with the economic 'drawable_outcome' settlement label UNCHANGED — so the settlement-equivalence
    allowlist key ('KXWCGAME','drawable_outcome') gates it WITHOUT expansion.
    ⚠️ This pins the matching MECHANICS only — admitting the type assumes 90'-regulation settlement,
    which is UNVERIFIED and blocks ship (see _SOCCER_FULL_TIME_TYPE)."""
    ev = _event()
    for m in ev["markets"]:
        m["sportsMarketType"] = "soccer_team_full_time_winner"
        m["feeCoefficient"] = 0.03                  # non-default → proves the fee predicate matches the rename
    pairs = await PolyUSScanner(_FakeSDK([ev])).fetch_markets(["69"])
    assert len(pairs) == 1
    p = pairs[0]
    assert p.settlement_type == "drawable_outcome"  # economic label UNCHANGED → allowlist key intact
    assert p.token_yes_a == "atc-fwc-fra-sen-2026-06-16-fra"
    assert p.token_yes_b == "atc-fwc-fra-sen-2026-06-16-sen"
    assert p.kalshi_series == "KXWCGAME"            # seriesId 69 → KXWCGAME (sport-consistency stamp)
    assert p.taker_fee_rate == pytest.approx(0.03)  # fee READ from the renamed market (fee predicate updated)


def test_is_drawable_market_type_accepts_rename_rejects_knockout():
    from bot.poly_us.scanner import _is_drawable_market_type
    assert _is_drawable_market_type("drawable_outcome")               # legacy
    assert _is_drawable_market_type("soccer_team_full_time_winner")   # the 2026-06-24 rename
    # knockout / non-winner / other-sport types are NOT drawable-equivalent → rejected
    # (knockout resolves on ET/pens; moneyline is the 2-way path):
    for bad in ("single_winner", "soccer_team_knockout_winner", "moneyline",
                "baseball_team_full_game_winner", "futures", None, 123):
        assert not _is_drawable_market_type(bad)


@pytest.mark.asyncio
async def test_moneyline_requires_both_long_and_short_side():
    """A moneyline market missing a side can't form a hedge → no pair."""
    ev = _mlb_event()
    ev["markets"][0]["marketSides"] = ev["markets"][0]["marketSides"][:1]  # only long
    pairs = await PolyUSScanner(_FakeSDK([ev])).fetch_markets(["15"])
    assert pairs == []


@pytest.mark.asyncio
async def test_fetch_markets_paginates_beyond_one_page():
    """Series with > one page of events must all be discovered (the bug that hid
    Iraq-Norway: only the first ~20 World Cup games were fetched)."""
    def _ev(i):
        e = _event()
        e["slug"] = f"fwc-game-{i}"
        e["title"] = f"Team{i}A vs. Team{i}B"
        for m, side in zip(e["markets"], ("a", "b")):
            ident = f"atc-fwc-game-{i}-{side}"
            m["slug"] = ident
            m["marketSides"][0]["identifier"] = ident
        return e
    events = [_ev(i) for i in range(250)]  # 3 pages at PAGE_SIZE=100
    scanner = PolyUSScanner(_FakeSDK(events))
    pairs = await scanner.fetch_markets(["69"])
    assert len(pairs) == 250  # all discovered, not just the first page


# ── poly_tick: read live per-market, and BLANK rather than defaulted when absent ──────────────
# The tick differs per MARKET, not per series, and decides which rungs poly_fillable sums. It is
# recorded so that regime change shows up in the data instead of as a surprise — which is exactly
# what it did on its first row. Values: the venue-reference skill; never restate them here.
# ⚠️ This comment used to assert "0.01 on every live series, 0.001 on NBA/NHL when they return
# ~Oct". Both halves were false (MLB is 0.005; 0.001 has never been observed anywhere) and never
# measured — the claim propagated from one unverified note into six files. Read the tick.
from bot.poly_us.scanner import _event_poly_tick, _is_drawable_market_type


@pytest.mark.asyncio
async def test_poly_tick_read_live_from_the_market():
    # A BARE FLOAT, which is what the venue actually sends — captured from the live event feed
    # 2026-07-15 (WorldCup seriesId=69, drawable_outcome: `orderPriceMinTickSize` = 0.01, a
    # float). NOT an {"value": ...} Amount wrapper: this field looks like a price but is not
    # shaped like one, and the neighbouring quote fields ARE wrapped, so the wrong guess is the
    # natural one.
    # The 0.001 below is deliberately a value the venue has NEVER been seen to send (the real
    # slate is 0.005/0.01). That's the point: it can only pass if we relay the venue's number
    # rather than defaulting, so it also can't quietly encode a tick claim that later turns out
    # wrong — which is how "NBA/NHL = 0.001" survived unmeasured in six files.
    ev = _event()
    for m in ev["markets"]:
        m["orderPriceMinTickSize"] = 0.001
    p = (await PolyUSScanner(_FakeSDK([ev])).fetch_markets(["69"]))[0]
    assert p.poly_tick == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_poly_tick_is_none_when_the_venue_does_not_report_it():
    """The OPPOSITE fail direction from the fee beside it, deliberately.

    A fee falls back to a default because the trade must be costed with SOMETHING. Nothing
    computes with the tick — so an absent one stays None. Defaulting to 0.01 would be right for
    every series live today and wrong for exactly the markets the column exists to catch, which
    is the worst case: a plausible value that never looks broken.
    """
    p = (await PolyUSScanner(_FakeSDK([_event()])).fetch_markets(["69"]))[0]
    assert p.poly_tick is None
    assert p.taker_fee_rate == _POLY_US_TAKER_THETA   # the fee DID fall back, from the same event


def test_event_poly_tick_parses_the_real_bare_float_and_never_guesses():
    def _ev(raw):
        return {"markets": [{"sportsMarketType": "drawable_outcome",
                             "orderPriceMinTickSize": raw}]}
    # THE REAL SHAPE [VERIFIED 2026-07-15 against the live event feed]: a bare float.
    assert _event_poly_tick(_ev(0.01), _is_drawable_market_type) == pytest.approx(0.01)
    # Defensive, NOT observed: an Amount wrapper and a numeric string. Kept because the quote
    # fields beside this one ARE wrapped, so the venue moving this one is a plausible drift —
    # and it would otherwise fail as a permanent silent blank rather than an error.
    assert _event_poly_tick(_ev({"value": "0.01", "currency": "USD"}),
                            _is_drawable_market_type) == pytest.approx(0.01)
    assert _event_poly_tick(_ev("0.001"), _is_drawable_market_type) == pytest.approx(0.001)
    # Unparseable → None; a non-positive tick is not a tick. Never a guess, never a crash.
    for bad in (None, 0, -0.01, "", "abc", {}, {"value": "abc"}, []):
        assert _event_poly_tick(_ev(bad), _is_drawable_market_type) is None
    assert _event_poly_tick({"markets": []}, _is_drawable_market_type) is None
