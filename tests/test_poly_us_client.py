"""Tests for PolyUSClient — parsing & translation against a mocked SDK."""
from datetime import datetime, timezone

import pytest

from bot.poly_us.client import (
    PolyUSClient, _amount_to_float, order_is_filled, order_filled_qty, transact_age_s,
    _parse_book_stats, _EMPTY_BOOK_STATS,
)


def test_order_is_filled_true_when_execution_state_filled():
    resp = {"id": "1", "executions": [{"order": {"state": "ORDER_STATE_FILLED"}}]}
    assert order_is_filled(resp) is True


def test_order_filled_qty_reads_cumquantity():
    # Poly coerces FOK→IOC; reconcile to actual filled qty (max cumQuantity).
    assert order_filled_qty(
        {"executions": [{"order": {"cumQuantity": 0}}, {"order": {"cumQuantity": 3}}]}
    ) == 3.0
    assert order_filled_qty({"executions": []}) == 0.0
    assert order_filled_qty(None) == 0.0


def test_order_filled_qty_ignores_the_lying_first_execution():
    """max() across executions is LOAD-BEARING — executions[0] LIES.

    Pinned to a REAL response [VERIFIED 2026-07-15, scripts/poly_fok_probe.py]: we asked for
    tif=FILL_OR_KILL, the exchange echoed IMMEDIATE_OR_CANCEL and PARTIAL-FILLED 255/300 —
    and the FIRST execution's order still carried cumQuantity=0 / leavesQuantity=300, with only
    a LATER execution carrying 255.

    Reading executions[0] returns 0 → "the leg missed" on a leg that filled 255. Under
    poly_first that books no hedge and leaves a naked, UNRECORDED Poly position — invisible to
    the tracker, the exposure caps and the strand alert. Never reduce this to first/last.
    """
    real = {
        "id": "B8G89C9MC75G",
        "executions": [
            {"id": "B8GTKWM4A6AH", "order": {
                "id": "B8G89C9MC75G", "marketSlug": "tec-mls-winner-2026-11-07-dcu",
                "side": "ORDER_SIDE_BUY", "type": "ORDER_TYPE_LIMIT",
                "price": {"value": "0.009", "currency": "USD"},
                "quantity": 300, "cumQuantity": 0, "leavesQuantity": 300,
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",   # we SENT FILL_OR_KILL
                "intent": "ORDER_INTENT_BUY_LONG"}},
            {"id": "B8GTKWM4A6AI", "order": {
                "id": "B8G89C9MC75G", "quantity": 300, "cumQuantity": 255,
                "leavesQuantity": 45, "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"}},
        ],
    }
    assert order_filled_qty(real) == 255.0, "must read the REAL fill, not the first execution"
    assert real["executions"][0]["order"]["cumQuantity"] == 0, "the trap this guards"


def test_order_is_filled_false_for_killed_fok():
    # A killed/expired FOK returns a dict but no filled execution.
    assert order_is_filled({"id": "1", "executions": []}) is False
    assert order_is_filled(
        {"id": "1", "executions": [{"order": {"state": "ORDER_STATE_CANCELED"}}]}
    ) is False


def test_order_is_filled_false_for_non_dict():
    assert order_is_filled(None) is False
    assert order_is_filled("oops") is False


def test_amount_to_float_parses_value():
    assert _amount_to_float({"value": "0.6500", "currency": "USD"}) == pytest.approx(0.65)


def test_amount_to_float_none_returns_none():
    assert _amount_to_float(None) is None


class _FakeMarkets:
    def __init__(self, bbo_resp):
        self._bbo_resp = bbo_resp
    async def bbo(self, slug):
        self._last_slug = slug
        return self._bbo_resp


class _FakeAccount:
    def __init__(self, balances_resp):
        self._balances_resp = balances_resp
    async def balances(self):
        return self._balances_resp


@pytest.mark.asyncio
async def test_get_best_ask_parses_marketdata(monkeypatch):
    client = PolyUSClient.__new__(PolyUSClient)  # bypass __init__/auth
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeMarkets(
        {"marketData": {"marketSlug": "x", "bestAsk": {"value": "0.2400", "currency": "USD"}}}
    )
    ask = await client.get_best_ask("x")
    assert ask == pytest.approx(0.24)


@pytest.mark.asyncio
async def test_get_fill_ask_long_returns_best_ask():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeMarkets(
        {"marketData": {"bestAsk": {"value": "0.4800", "currency": "USD"},
                        "bestBid": {"value": "0.4700", "currency": "USD"}}}
    )
    assert await client.get_fill_ask("aec-mlb-tor-bos") == pytest.approx(0.48)


@pytest.mark.asyncio
async def test_get_fill_ask_short_returns_one_minus_bid():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeMarkets(
        {"marketData": {"bestAsk": {"value": "0.4800", "currency": "USD"},
                        "bestBid": {"value": "0.4700", "currency": "USD"}}}
    )
    # short side ask = 1 − bestBid = 0.53
    assert await client.get_fill_ask("aec-mlb-tor-bos::short") == pytest.approx(0.53)


@pytest.mark.asyncio
async def test_get_fill_ask_none_on_error():
    class _BoomMarkets:
        async def bbo(self, slug):
            raise RuntimeError("network")
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _BoomMarkets()
    assert await client.get_fill_ask("slug") is None       # fail-closed


class _FakeSettlementMarkets:
    def __init__(self, resp):
        self._resp = resp
    async def settlement(self, slug):
        self._last_slug = slug
        return self._resp


@pytest.mark.asyncio
async def test_get_settlement_parses_numeric_field():
    # Live API returns {"slug":..,"settlement":<number>} (NOT settlementPrice:Amount).
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeSettlementMarkets({"slug": "x", "settlement": 1})
    assert await client.get_settlement("aec-mlb-tor-bos") == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_get_settlement_zero_is_valid_not_missing():
    # A definite long-LOSS settles at 0 (falsy); must NOT read as not-yet-settled.
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeSettlementMarkets({"slug": "x", "settlement": 0})
    assert await client.get_settlement("aec-mlb-tor-bos::short") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_get_settlement_amount_dict_fallback():
    # Tolerate the SDK-typed settlementPrice:Amount shape too.
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeSettlementMarkets(
        {"settlementPrice": {"value": "0.4300", "currency": "USD"}}
    )
    assert await client.get_settlement("x") == pytest.approx(0.43)


@pytest.mark.asyncio
async def test_get_settlement_missing_returns_none():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeSettlementMarkets({"slug": "x"})   # no settlement field
    assert await client.get_settlement("x") is None


class _FakeBookMarkets:
    def __init__(self, resp):
        self._resp = resp
    async def book(self, slug):
        return self._resp


def _book(state, offers=None, bids=None):
    md = {"state": state}
    if offers is not None:
        md["offers"] = [{"px": {"value": f"{p}", "currency": "USD"}, "qty": "100"} for p in offers]
    if bids is not None:
        md["bids"] = [{"px": {"value": f"{p}", "currency": "USD"}, "qty": "100"} for p in bids]
    return {"marketData": md}


@pytest.mark.asyncio
async def test_get_fill_quote_long_open():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeBookMarkets(_book("MARKET_STATE_OPEN", offers=[0.48, 0.49]))
    ask, state, levels, _tx, _stats = await client.get_fill_quote("aec-mlb-tor-bos")
    assert ask == pytest.approx(0.48) and state == "MARKET_STATE_OPEN"
    # long: offers as-is in ask-space, both levels carried (caller sums fillable-at-limit)
    assert levels == [(0.48, 100.0), (0.49, 100.0)]


@pytest.mark.asyncio
async def test_get_fill_quote_short_one_minus_bid():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeBookMarkets(_book("MARKET_STATE_OPEN", bids=[0.47, 0.46]))
    ask, state, levels, _tx, _stats = await client.get_fill_quote("aec-mlb-tor-bos::short")
    assert ask == pytest.approx(0.53)   # 1 − best bid 0.47
    # short: bids normalized to ASK space (1−px), so the caller's `p <= poly_limit` is
    # apples-to-apples — this is the one place a space-mismatch would hide.
    assert levels == [(0.53, 100.0), (0.54, 100.0)]


@pytest.mark.asyncio
async def test_get_fill_quote_reports_suspended_state():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeBookMarkets(_book("MARKET_STATE_SUSPENDED", offers=[0.38]))
    ask, state, _levels, _tx, _stats = await client.get_fill_quote("aec-fwc-sui-bih")
    assert state == "MARKET_STATE_SUSPENDED"   # caller must refuse this


@pytest.mark.asyncio
async def test_get_fill_quote_failclosed_on_error():
    class _BoomMarkets:
        async def book(self, slug):
            raise RuntimeError("network")
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _BoomMarkets()
    res = await client.get_fill_quote("slug")
    assert res[:4] == (None, "?", [], None)            # fail-closed (4 fire-path values)
    assert all(v is None for v in res[4].values())     # stats all-None, never raises


# ── cache-bust (fresh=) + transactTime ───────────────────────────────────────────────

class _FakeSDK:
    """SDK with both the resource method (markets.book) and the raw .get() the fresh path
    uses; both return the SAME book so the two read paths can be compared structurally."""
    def __init__(self, book):
        self._book = book
        self.markets = _FakeBookMarkets(book)
        self.get_calls = []
    async def get(self, path, *, query=None):
        self.get_calls.append((path, query))
        return self._book


@pytest.mark.asyncio
async def test_get_fill_quote_returns_transact_time():
    book = _book("MARKET_STATE_OPEN", offers=[0.48])
    book["marketData"]["transactTime"] = "2026-06-22T19:30:20.818756170Z"
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK(book)
    _ask, _state, _levels, tx, _stats = await client.get_fill_quote("aec-mlb-tor-bos")
    assert tx == "2026-06-22T19:30:20.818756170Z"


# ── marketData.stats → liquidity/activity (logging-only; FREE on the same fetch) ──────────────

def test_parse_book_stats_present_with_ages():
    md = {"stats": {
        "openInterest": "507004.69",
        "openInterestSetTime": "2026-06-23T00:00:00.000000Z",
        "lastTradePx": {"value": "0.8500", "currency": "USD"},
        "lastTradeQty": "9.34",
        "lastTradeSetTime": "2026-06-23T00:00:00.000000Z",
        "sharesTraded": "530166.54",
        "notionalTraded": {"value": "44274039.23", "currency": "USD"},
    }}
    now = datetime(2026, 6, 23, 0, 0, 0, tzinfo=timezone.utc).timestamp() + 5.0
    s = _parse_book_stats(md, now)
    assert s["open_interest"] == pytest.approx(507004.69)
    assert s["last_trade_px"] == pytest.approx(0.85)
    assert s["last_trade_qty"] == pytest.approx(9.34)
    assert s["shares_traded"] == pytest.approx(530166.54)
    assert s["notional_traded"] == pytest.approx(44274039.23)
    assert s["oi_age_s"] == pytest.approx(5.0)            # now − openInterestSetTime
    assert s["last_trade_age_s"] == pytest.approx(5.0)


def test_parse_book_stats_absent_is_all_none_and_never_raises():
    # No / garbled / missing stats → all-None, no raise (logging must never break the fire path).
    assert _parse_book_stats({}, 1000.0) == _EMPTY_BOOK_STATS
    assert _parse_book_stats({"stats": "garbage"}, 1000.0) == _EMPTY_BOOK_STATS
    assert _parse_book_stats(None, 1000.0) == _EMPTY_BOOK_STATS
    # one unparseable field → None for it, the others still parse (independent).
    s = _parse_book_stats({"stats": {"openInterest": "abc", "sharesTraded": "12"}}, 1000.0)
    assert s["open_interest"] is None and s["shares_traded"] == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_get_fill_quote_carries_stats():
    book = _book("MARKET_STATE_OPEN", offers=[0.48])
    book["marketData"]["stats"] = {"openInterest": "777",
                                   "lastTradePx": {"value": "0.48", "currency": "USD"}}
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK(book)
    _ask, _state, _levels, _tx, stats = await client.get_fill_quote("aec-mlb-tor-bos")
    assert stats["open_interest"] == pytest.approx(777.0)
    assert stats["last_trade_px"] == pytest.approx(0.48)


@pytest.mark.asyncio
async def test_get_fill_quote_fresh_busts_cache_via_nonce_get():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    sdk = client._sdk = _FakeSDK(_book("MARKET_STATE_OPEN", offers=[0.48]))
    await client.get_fill_quote("aec-mlb-tor-bos")            # default → resource method
    assert sdk.get_calls == []                                # no raw .get()
    await client.get_fill_quote("aec-mlb-tor-bos", fresh=True)  # fresh → raw .get() w/ nonce
    assert len(sdk.get_calls) == 1
    path, query = sdk.get_calls[0]
    assert path == "/v1/markets/aec-mlb-tor-bos/book"
    assert "_" in query and query["_"]                        # nonce present, non-empty


@pytest.mark.asyncio
async def test_get_fill_quote_cached_and_fresh_paths_structurally_equivalent():
    """The cache-bust must change ONLY the cache key, never the parsed shape — else the fresh
    fire-path read silently produces different asks/depths (a money-path bug). Same book through
    both branches → identical (ask, state, levels, transact_time)."""
    book = _book_q("MARKET_STATE_OPEN", offers=[(0.48, 40), (0.49, 100)])
    book["marketData"]["transactTime"] = "2026-06-22T19:30:20.818000000Z"
    c1 = PolyUSClient.__new__(PolyUSClient); c1._dry_run = False; c1._sdk = _FakeSDK(book)
    c2 = PolyUSClient.__new__(PolyUSClient); c2._dry_run = False; c2._sdk = _FakeSDK(book)
    cached = await c1.get_fill_quote("aec-mlb-tor-bos")              # markets.book path
    fresh = await c2.get_fill_quote("aec-mlb-tor-bos", fresh=True)   # nonce .get() path
    assert cached == fresh
    assert cached[0] == pytest.approx(0.48) and cached[3] == "2026-06-22T19:30:20.818000000Z"


def test_transact_age_s_parses_ns_iso():
    # ns fraction truncated to µs; age = now − transactTime.
    tx = "2026-06-22T19:30:20.818756170Z"
    base = datetime(2026, 6, 22, 19, 30, 20, 818756, tzinfo=timezone.utc).timestamp()
    assert transact_age_s(tx, base + 10.0) == pytest.approx(10.0, abs=1e-3)


def test_transact_age_s_none_on_missing_or_junk():
    assert transact_age_s(None, 1000.0) is None
    assert transact_age_s("", 1000.0) is None
    assert transact_age_s("not-a-timestamp", 1000.0) is None


# ── would-fire sampler depth comparability ──────────────────────────────────────────
# The sampler (kalshi_arb._sample_book_evolution) switched its REST source from
# get_book_depth → get_fill_quote so one fetch yields the REST ask AND best-level depth.
# rest_poly_depth must stay equal to the old get_book_depth value or the WS-vs-REST depth
# comparison silently changes meaning. SHORT tokens are where a bid-space/ask-space mismatch
# would hide, so this is a PERMANENT guard (not a one-time live eyeball that may skip short).

def _book_q(state, offers=None, bids=None):
    """Like _book but per-level qty: pass (px, qty) tuples."""
    md = {"state": state}
    if offers is not None:
        md["offers"] = [{"px": {"value": f"{p}", "currency": "USD"}, "qty": f"{q}"} for p, q in offers]
    if bids is not None:
        md["bids"] = [{"px": {"value": f"{p}", "currency": "USD"}, "qty": f"{q}"} for p, q in bids]
    return {"marketData": md}


def _sampler_best_level_depth(levels):
    """The exact best-level reduction the sampler runs on get_fill_quote's ask_levels:
    best = min ask price (drawn from the list, so the minimal level satisfies p <= best
    exactly — no fragile float-equality), sum qty at-or-below it."""
    if not levels:
        return None
    best = min(p for p, _ in levels)
    return sum(q for p, q in levels if p <= best)


@pytest.mark.asyncio
async def test_sampler_depth_matches_get_book_depth_long():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeBookMarkets(
        _book_q("MARKET_STATE_OPEN", offers=[(0.48, 40), (0.49, 100)])
    )
    old = await client.get_book_depth("aec-mlb-tor-bos")
    _ask, _state, levels, _tx, _stats = await client.get_fill_quote("aec-mlb-tor-bos")
    assert old == _sampler_best_level_depth(levels) == 40.0   # qty at best offer 0.48


@pytest.mark.asyncio
async def test_sampler_depth_matches_get_book_depth_short():
    # Short bid 0.30 (qty 40) is an ASK at 1−0.30=0.70. get_book_depth sums qty at the
    # MAX bid (0.30); the new path sums qty at the MIN ask (0.70) — same physical level,
    # so the same qty. This is the case the comparability claim could be quietly false on.
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeBookMarkets(
        _book_q("MARKET_STATE_OPEN", bids=[(0.30, 40), (0.29, 100)])
    )
    old = await client.get_book_depth("aec-mlb-tor-bos::short")
    _ask, _state, levels, _tx, _stats = await client.get_fill_quote("aec-mlb-tor-bos::short")
    assert _ask == pytest.approx(0.70)                       # 1 − best bid 0.30
    assert old == _sampler_best_level_depth(levels) == 40.0  # space-invariant qty


@pytest.mark.asyncio
async def test_get_best_ask_none_when_no_ask(monkeypatch):
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeMarkets({"marketData": {"bestAsk": None}})
    assert await client.get_best_ask("x") is None


@pytest.mark.asyncio
async def test_get_usdc_balance_returns_buying_power():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.account = _FakeAccount(
        {"balances": [{"currentBalance": 200.0, "buyingPower": 187.62, "currency": "USD"}]}
    )
    assert await client.get_usdc_balance() == pytest.approx(187.62)


@pytest.mark.asyncio
async def test_get_usdc_balance_dry_run_returns_sentinel():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = True
    assert await client.get_usdc_balance() == 9999.0


def test_amount_to_float_malformed_returns_none():
    assert _amount_to_float({"value": "not-a-number"}) is None
    assert _amount_to_float({}) is None


@pytest.mark.asyncio
async def test_get_usdc_balance_no_usd_balance_returns_zero():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.account = _FakeAccount({"balances": [{"currency": "EUR", "buyingPower": 50.0}]})
    assert await client.get_usdc_balance() == 0.0


@pytest.mark.asyncio
async def test_get_usdc_balance_error_returns_negative_one():
    class _BoomAccount:
        async def balances(self):
            raise RuntimeError("network down")
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.account = _BoomAccount()
    assert await client.get_usdc_balance() == -1.0


class _FakeOrders:
    def __init__(self, resp):
        self._resp = resp
        self.last_params = None
    async def create(self, params):
        self.last_params = params
        return self._resp


@pytest.mark.asyncio
async def test_place_limit_fok_dry_run_does_not_call_sdk():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = True
    client._sdk = type("S", (), {})()
    client._sdk.orders = _FakeOrders({"order": {"status": "should_not_be_used"}})
    resp = await client.place_limit_fok("slug-x", 0.24, 100, "[t]")
    assert resp["status"] == "dry_run"
    assert client._sdk.orders.last_params is None


@pytest.mark.asyncio
async def test_place_limit_fok_builds_fok_buy_long_order():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.orders = _FakeOrders({"order": {"status": "killed"}})
    await client.place_limit_fok("slug-x", 0.24, 100, "[t]")
    p = client._sdk.orders.last_params
    assert p["marketSlug"] == "slug-x"
    assert p["intent"] == "ORDER_INTENT_BUY_LONG"
    assert p["type"] == "ORDER_TYPE_LIMIT"
    # IOC, not FOK: Poly does not honor FOK — it silently rewrites it to IOC [VERIFIED
    # 2026-07-15 on a real order, 255/300 partial]. We now send what we actually get, so a
    # future Poly FOK rollout cannot silently turn our orders all-or-nothing.
    assert p["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert p["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC"
    assert p["price"] == {"value": "0.2400", "currency": "USD"}
    assert p["quantity"] == 100


@pytest.mark.asyncio
async def test_place_limit_fok_RAISES_on_error_because_an_error_is_not_an_outcome():
    """It returned None, and that is how "we don't know" became "filled nothing".

    None flowed to order_filled_qty → 0.0 → _exec_poly_first's `P <= 0` branch → "MISSED (no
    fill)", under a comment reading "no exposure". But only timeout / 502 / reset reach here — a
    real IOC kill is a 200 with cumQuantity 0 — and `synchronousExecution` blocks the order ~61ms
    server-side, so a timeout lands INSIDE the fill window. Poly fires FIRST in the deployed
    config; the Kalshi client states this same rule for itself ("a FOK kill is an OUTCOME, NOT AN
    ERROR") and raises.

    Callers must be able to tell "the venue said zero" from "we do not know". Raising is the only
    way to say the second.
    """
    class _BoomOrders:
        async def create(self, params):
            raise RuntimeError("timeout")
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.orders = _BoomOrders()
    with pytest.raises(RuntimeError, match="timeout"):
        await client.place_limit_fok("slug-x", 0.24, 100, "[t]")


@pytest.mark.asyncio
async def test_place_limit_fok_still_returns_a_zero_fill_response_for_a_REAL_kill():
    """The other half: when the venue ANSWERS and fills nothing, that is an outcome, not an error.
    It must come back as a response so the normal P<=0 path runs — not as a raise."""
    class _KillOrders:
        async def create(self, params):
            return {"state": "ORDER_STATE_CANCELED", "executions": [{"cumQuantity": "0"}]}
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.orders = _KillOrders()
    resp = await client.place_limit_fok("slug-x", 0.24, 100, "[t]")
    assert resp is not None and order_filled_qty(resp) == 0.0


@pytest.mark.asyncio
async def test_sell_back_dry_run_returns_mock_price():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = True
    client._sdk = type("S", (), {})()
    assert await client.sell_back("slug-x", 100, "[t]") == (0.50, 100.0)


@pytest.mark.asyncio
async def test_sell_back_no_bids_returns_none():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK({"marketData": {"bids": []}})   # unwind reads via the fresh path
    # (price, sold) — a BARE None here would TypeError in _unwind_poly_excess's unpack
    assert await client.sell_back("slug-x", 100, "[t]") == (None, 0.0)


@pytest.mark.asyncio
async def test_sell_back_reads_fresh_book_not_cdn_cache():
    # The unwind price MUST come from the live book — a 30s-cached price could miss the real
    # book and strand the leg. Assert sell_back routes through the nonce cache-bust (_sdk.get).
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    sdk = client._sdk = _FakeSDK({"marketData": {"bids": [
        {"px": {"value": "0.60", "currency": "USD"}, "qty": "10"}]}})
    sdk.orders = _FakeOrders({"executions": [{"order": {"state": "ORDER_STATE_FILLED"}}]})
    await client.sell_back("slug-x", 100, "[t]")
    assert len(sdk.get_calls) == 1                          # used .get(), not markets.book
    path, query = sdk.get_calls[0]
    assert path == "/v1/markets/slug-x/book" and "_" in query and query["_"]


@pytest.mark.asyncio
async def test_sell_back_sells_at_best_bid():
    class _Orders:
        def __init__(self):
            self.calls = []
        async def create(self, params):
            self.calls.append(params)
            return {"executions": [{"order": {"state": "ORDER_STATE_FILLED"}}]}
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK({"marketData": {"bids": [
        {"px": {"value": "0.60", "currency": "USD"}, "qty": "10"},
        {"px": {"value": "0.55", "currency": "USD"}, "qty": "10"},
    ]}})
    client._sdk.orders = _Orders()
    price, sold = await client.sell_back("slug-x", 100, "[t]")
    assert price == pytest.approx(0.60)  # best bid
    assert sold == 100.0
    p = client._sdk.orders.calls[0]
    assert p["intent"] == "ORDER_INTENT_SELL_LONG"
    # IOC, not FOK: Poly does not honor FOK — it silently rewrites it to IOC [VERIFIED
    # 2026-07-15 on a real order, 255/300 partial]. We now send what we actually get, so a
    # future Poly FOK rollout cannot silently turn our orders all-or-nothing.
    assert p["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert p["price"] == {"value": "0.6000", "currency": "USD"}
    assert p["quantity"] == 100


@pytest.mark.asyncio
async def test_place_limit_fok_short_token_uses_buy_short_and_strips_suffix():
    """A '<slug>::short' token → BUY_SHORT on the bare slug at the given short price."""
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = type("S", (), {})()
    client._sdk.orders = _FakeOrders({"order": {"status": "killed"}})
    await client.place_limit_fok("aec-mlb-tor-bos::short", 0.53, 50, "[t]")
    p = client._sdk.orders.last_params
    assert p["marketSlug"] == "aec-mlb-tor-bos"          # suffix stripped
    assert p["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert p["price"] == {"value": "0.5300", "currency": "USD"}
    assert p["quantity"] == 50


@pytest.mark.asyncio
async def test_sell_back_short_position_sells_short_at_one_minus_ask():
    """Unwinding a SHORT leg: SELL_SHORT at the short bid = 1 − best long ask
    (crosses the offers, not the bids). Preserves the stranded-leg invariant."""
    class _Orders:
        def __init__(self):
            self.calls = []
        async def create(self, params):
            self.calls.append(params)
            return {"executions": [{"order": {"state": "ORDER_STATE_FILLED"}}]}
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK({"marketData": {"offers": [
        {"px": {"value": "0.40", "currency": "USD"}, "qty": "10"},
        {"px": {"value": "0.45", "currency": "USD"}, "qty": "10"},
    ]}})
    client._sdk.orders = _Orders()
    price, sold = await client.sell_back("aec-mlb-tor-bos::short", 30, "[t]")
    assert price == pytest.approx(0.60)  # 1 − 0.40 best ask
    p = client._sdk.orders.calls[0]
    assert p["marketSlug"] == "aec-mlb-tor-bos"
    assert p["intent"] == "ORDER_INTENT_SELL_SHORT"
    assert p["price"] == {"value": "0.6000", "currency": "USD"}


@pytest.mark.asyncio
async def test_sell_back_retries_at_discount_when_first_fails():
    class _Orders:
        def __init__(self):
            self.calls = []
        async def create(self, params):
            self.calls.append(params)
            # First attempt (best bid) raises; second (discount) succeeds.
            if len(self.calls) == 1:
                raise RuntimeError("FOK missed")
            return {"executions": [{"order": {"state": "ORDER_STATE_FILLED"}}]}
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK(
        {"marketData": {"bids": [{"px": {"value": "0.60", "currency": "USD"}, "qty": "10"}]}})
    client._sdk.orders = _Orders()
    price, sold = await client.sell_back("slug-x", 100, "[t]")
    assert price == pytest.approx(0.58)  # 0.60 - 0.02
    assert len(client._sdk.orders.calls) == 2
    assert client._sdk.orders.calls[1]["price"] == {"value": "0.5800", "currency": "USD"}


# ── get_book_depth: authoritative REST depth for the would-fire sampler ──────────

@pytest.mark.asyncio
async def test_get_book_depth_sums_qty_at_best_level_long():
    client = PolyUSClient.__new__(PolyUSClient)
    client._sdk = type("S", (), {})()
    # two offers at the best price 0.24 (summed), one deeper at 0.25 (ignored). qty=100 each.
    client._sdk.markets = _FakeBookMarkets(_book("MARKET_STATE_OPEN", offers=[0.24, 0.24, 0.25]))
    assert await client.get_book_depth("slug-x") == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_get_book_depth_short_uses_best_bid_level():
    client = PolyUSClient.__new__(PolyUSClient)
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeBookMarkets(_book("MARKET_STATE_OPEN", bids=[0.47, 0.46]))
    assert await client.get_book_depth("aec-mlb-tor-bos::short") == pytest.approx(100.0)  # best bid 0.47 only


@pytest.mark.asyncio
async def test_get_book_depth_none_on_empty_and_error():
    client = PolyUSClient.__new__(PolyUSClient)
    client._sdk = type("S", (), {})()
    client._sdk.markets = _FakeBookMarkets(_book("MARKET_STATE_OPEN", offers=[]))
    assert await client.get_book_depth("slug-x") is None

    class _Boom:
        async def book(self, slug):
            raise RuntimeError("network")
    client._sdk.markets = _Boom()
    assert await client.get_book_depth("slug-x") is None       # fail-closed


# ── sell_back partial fills (the IOC consequence) ─────────────────────────────────────────────
# Poly rewrites our FOK->IOC [VERIFIED 2026-07-15 on a real order], so a SELL can partial-fill.
# sell_back's "try best, retry 2c worse" design assumed all-or-nothing: attempt 1 either filled
# completely or did nothing. It doesn't. Pin the real state space.

class _PartialOrders:
    """Fills `fills` in order, one per create() call. Records each request."""
    def __init__(self, fills):
        self.fills, self.calls = list(fills), []

    async def create(self, params):
        self.calls.append(params)
        want = int(params["quantity"])
        got = min(self.fills.pop(0) if self.fills else 0, want)
        state = "ORDER_STATE_FILLED" if got >= want else "ORDER_STATE_PARTIALLY_FILLED"
        return {"executions": [{"order": {
            "state": state, "quantity": want, "cumQuantity": got}}]}


def _bid_book(px="0.60"):
    return {"marketData": {"bids": [{"px": {"value": px, "currency": "USD"}, "qty": "1000"}]}}


@pytest.mark.asyncio
async def test_sell_back_reports_the_quantity_actually_sold():
    """sell_back must report HOW MUCH sold, not just a price. A caller that only learns a price
    cannot tell a full sale from a partial one — and _unwind_poly_excess must strand only the
    unsold remainder."""
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK(_bid_book())
    client._sdk.orders = _PartialOrders([100])          # sells all 100 first try
    price, sold = await client.sell_back("slug-x", 100, "[t]")
    assert sold == 100.0
    assert price == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_sell_back_retry_resizes_to_the_UNSOLD_remainder():
    """THE BUG: the retry closed over the ORIGINAL size. After attempt 1 partial-fills 60/100,
    attempt 2 re-sent quantity=100 while only 40 were still held — an OVERSELL. It must ask for
    the 40 that are left."""
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK(_bid_book())
    client._sdk.orders = _PartialOrders([60, 40])       # 60 at best, 40 on the retry
    price, sold = await client.sell_back("slug-x", 100, "[t]")
    assert [c["quantity"] for c in client._sdk.orders.calls] == [100, 40], \
        "retry must re-size to the unsold remainder, never re-send the original size"
    assert sold == 100.0


@pytest.mark.asyncio
async def test_sell_back_partial_on_both_attempts_reports_what_sold():
    """Neither attempt clears it: we still SOLD 80. Reporting 'failed / nothing sold' would
    strand 100 phantom shares and lose the proceeds of 80 from P&L."""
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK(_bid_book())
    client._sdk.orders = _PartialOrders([60, 20])       # 60 + 20 = 80 of 100
    price, sold = await client.sell_back("slug-x", 100, "[t]")
    assert sold == 80.0
    assert price is not None, "80 shares really sold — a price must be reported for the P&L"


@pytest.mark.asyncio
async def test_sell_back_nothing_sold_reports_zero():
    client = PolyUSClient.__new__(PolyUSClient)
    client._dry_run = False
    client._sdk = _FakeSDK(_bid_book())
    client._sdk.orders = _PartialOrders([0, 0])
    price, sold = await client.sell_back("slug-x", 100, "[t]")
    assert (price, sold) == (None, 0.0)


# ── quote_from_md: the parser get_fill_quote is a fetch around ────────────────────────────────
# Extracted so the would-fire sampler can read a book it already holds in BOTH directions (ask
# here, bid via kalshi_arb._poly_exit_from_book) off ONE fetch. Poly allows ~1 req/s sustained and
# THROTTLES over-limit with a late/stale 200, so a second fetch for the bid would have degraded
# the freshness of the ask read beside it — on a budget the fire path shares.
import bot.poly_us.client as client_mod
from bot.poly_us.client import quote_from_md
from bot.poly_us.sides import parse_token


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["aec-mlb-tor-bos", "aec-mlb-tor-bos::short"])
async def test_quote_from_md_is_exactly_what_get_fill_quote_returns(token, monkeypatch):
    """The anti-drift property, stated as an assertion.

    The sampler reads through quote_from_md and the fire path through get_fill_quote. If those
    could disagree, the sampler would be recording a bot we do not run — so pin that one IS the
    other, over both token orientations (short is where a space-mismatch would hide).

    The clock is pinned and the book carries a `stats` block ON PURPOSE. quote_from_md is not
    pure — it ages the stats off time.time() — so without both, the 5th tuple element is all-None
    on either side, the comparison passes trivially while pinning nothing about stats, and adding
    a realistic fixture later would turn it into a flake instead of a failure.
    """
    monkeypatch.setattr(client_mod.time, "time", lambda: 1_800_000_000.0)
    book = _book("MARKET_STATE_OPEN", offers=[0.48, 0.49], bids=[0.47, 0.46])
    book["marketData"]["stats"] = {
        "openInterest": "1200",
        "openInterestSetTime": "2026-07-15T20:00:00.000000000Z",
        "lastTradePx": {"value": "0.4850", "currency": "USD"},
        "lastTradeSetTime": "2026-07-15T20:00:05.000000000Z",
    }
    c = PolyUSClient.__new__(PolyUSClient)
    c._dry_run = False
    c._sdk = type("S", (), {})()
    c._sdk.markets = _FakeBookMarkets(book)

    via_fetch = await c.get_fill_quote(token)
    _slug, is_short = parse_token(token)
    direct = quote_from_md(book["marketData"], is_short)
    assert direct == via_fetch
    assert via_fetch[4]["open_interest"] == 1200.0      # stats really were parsed, not all-None
    assert via_fetch[4]["oi_age_s"] is not None


def test_quote_from_md_fails_soft_on_a_junk_marketdata():
    # It rides an observability read; a malformed book must yield the no-quote tuple, not raise.
    ask, state, levels, tx, stats = quote_from_md({}, False)
    assert (ask, state, levels, tx) == (None, "?", [], None)
    assert stats["open_interest"] is None
