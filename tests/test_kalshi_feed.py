"""Tests for KalshiOrderBookCache WebSocket price cache."""
import asyncio
import json
import pytest
from unittest.mock import MagicMock


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cache():
    from bot.kalshi.feed import KalshiOrderBookCache
    client_mock = MagicMock()
    client_mock.ws_url = "wss://fake"
    client_mock.ws_headers.return_value = {}
    return KalshiOrderBookCache(client_mock, series_prefixes=["KXNBA"])


def _ticker_msg(market_ticker, yes_bid="0.4500", yes_ask="0.5500"):
    return json.dumps({
        "type": "ticker",
        "msg": {
            "market_ticker": market_ticker,
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": yes_ask,
        }
    })


def test_parse_ticker_updates_yes_prices(cache):
    _run(cache._handle_message(_ticker_msg("KXNBAGAME-LAKCEL-JUN14")))
    assert cache.get_best_bid("KXNBAGAME-LAKCEL-JUN14", "yes") == pytest.approx(0.45)
    assert cache.get_best_ask("KXNBAGAME-LAKCEL-JUN14", "yes") == pytest.approx(0.55)


def test_parse_ticker_derives_no_prices(cache):
    _run(cache._handle_message(_ticker_msg("KXNBAGAME-LAKCEL-JUN14", "0.4000", "0.6000")))
    # no_bid = 1 - yes_ask = 1 - 0.60 = 0.40
    assert cache.get_best_bid("KXNBAGAME-LAKCEL-JUN14", "no") == pytest.approx(0.40)
    # no_ask = 1 - yes_bid = 1 - 0.40 = 0.60
    assert cache.get_best_ask("KXNBAGAME-LAKCEL-JUN14", "no") == pytest.approx(0.60)


def test_unknown_ticker_returns_none(cache):
    assert cache.get_best_ask("KXNBAGAME-UNKNOWN", "yes") is None
    assert cache.get_best_bid("KXNBAGAME-UNKNOWN", "no") is None


def test_series_prefix_filter_blocks_unrelated(cache):
    """Ticker not starting with a tracked series prefix must be ignored."""
    msg = _ticker_msg("KXFEDDECISION-DEC25-T5.00")  # not in ["KXNBA"]
    _run(cache._handle_message(msg))
    assert cache.get_best_ask("KXFEDDECISION-DEC25-T5.00", "yes") is None


def test_series_prefix_filter_passes_matching(cache):
    msg = _ticker_msg("KXNBAGAME-BOS-JUN20")
    _run(cache._handle_message(msg))
    assert cache.get_best_ask("KXNBAGAME-BOS-JUN20", "yes") is not None


def test_callback_triggered_on_update(cache):
    called = []
    cache.set_callback(lambda: called.append(1))
    _run(cache._handle_message(_ticker_msg("KXNBAGAME-LAKCEL-JUN14")))
    assert len(called) == 1


def test_malformed_message_ignored(cache):
    _run(cache._handle_message("not-json"))
    _run(cache._handle_message('{"type":"unknown"}'))
    assert cache.get_best_ask("anything", "yes") is None


# ── live liquidity from the ticker stream (logging-only; isolated from the price path) ─────────

_TKR = "KXNBAGAME-LAKCEL-JUN14"


def _ticker_with_liq(market_ticker=_TKR, *, bid="0.4500", ask="0.5500",
                     oi="68.00", vol="94.00", last="0.5400", with_bidask=True):
    m = {"market_ticker": market_ticker, "open_interest_fp": oi,
         "volume_fp": vol, "price_dollars": last}
    if with_bidask:
        m["yes_bid_dollars"] = bid
        m["yes_ask_dollars"] = ask
    return json.dumps({"type": "ticker", "msg": m})


def test_ticker_records_live_liquidity(cache):
    _run(cache._handle_message(_ticker_with_liq()))
    liq = cache.get_liquidity(_TKR)
    assert liq is not None
    oi, vol, last, age = liq
    assert oi == pytest.approx(68.0) and vol == pytest.approx(94.0) and last == pytest.approx(0.54)
    assert 0.0 <= age < 5.0                       # fresh: receive-time anchored


def test_liquidity_parse_error_does_not_eat_price_write(cache):
    # FAIL-DIRECTION: a malformed liquidity field must NOT skip the bid/ask price update.
    _run(cache._handle_message(_ticker_with_liq(oi="abc")))   # open_interest_fp unparseable
    assert cache.get_best_ask(_TKR, "yes") == pytest.approx(0.55)   # price STILL written
    assert cache.get_liquidity(_TKR) is None                        # liquidity blank, not faked


def test_liquidity_recorded_even_when_bid_ask_missing(cache):
    # Independence (other direction): a tick with liquidity but no bid/ask still records liquidity;
    # the price is (correctly) not updated.
    _run(cache._handle_message(_ticker_with_liq(with_bidask=False)))
    assert cache.get_liquidity(_TKR) is not None
    assert cache.get_best_ask(_TKR, "yes") is None


def test_liquidity_blank_on_resubscribe(cache):
    # BLANK-NOT-STALE: a (re)subscribe (e.g. a mode switch) clears the cache → blank, not frozen.
    _run(cache._handle_message(_ticker_with_liq()))
    assert cache.get_liquidity(_TKR) is not None

    class _FakeWS:
        async def send(self, _msg):
            pass
    _run(cache._send_subscribe(_FakeWS()))
    assert cache.get_liquidity(_TKR) is None


def test_unseen_ticker_liquidity_none(cache):
    assert cache.get_liquidity("KXNBAGAME-NEVER-SEEN") is None


# ── Frozen-book detection foundation: _note_price_change / _last_book_change_ts ─────────────────
# These three pins are load-bearing — the freshness watchdog (_stale_book_reconnect) keys entirely
# on _last_book_change_ts, which must advance ONLY on a real tradeable-price move. Mirrors the Poly
# feed's _note_book pins. The key is (yes_bid, yes_ask) (no_* are exact complements — 2 DOF).

def test_note_price_change_resend_is_not_a_change(cache):
    """Identical re-send (the frozen-feed signature): the socket looks alive but the book never
    moved → must NOT advance _last_book_change_ts."""
    _run(cache._handle_message(_ticker_msg(_TKR, "0.4500", "0.5500")))
    assert cache._book_changes == 1
    t1 = cache._last_book_change_ts
    _run(cache._handle_message(_ticker_msg(_TKR, "0.4500", "0.5500")))   # identical re-send
    _run(cache._handle_message(_ticker_msg(_TKR, "0.4500", "0.5500")))   # still frozen
    assert cache._book_changes == 1                                      # no advance
    assert cache._last_book_change_ts == t1                              # stamp untouched


def test_note_price_change_liquidity_churn_is_not_a_change(cache):
    """The ticker-mode analog of Poly's 'deep churn ≠ change': a tick with the SAME yes_bid/yes_ask
    but DIFFERENT open_interest/volume keeps _last_msg_ts fresh (socket alive) but the tradeable
    price is frozen → must NOT advance _last_book_change_ts (else OI/volume churn masks a zombie)."""
    _run(cache._handle_message(_ticker_with_liq(bid="0.4500", ask="0.5500", oi="10", vol="20")))
    assert cache._book_changes == 1
    t1 = cache._last_book_change_ts
    # same price, churning liquidity:
    _run(cache._handle_message(_ticker_with_liq(bid="0.4500", ask="0.5500", oi="99", vol="180")))
    assert cache.get_liquidity(_TKR)[0] == pytest.approx(99.0)   # liquidity DID update (logging path)
    assert cache._book_changes == 1                              # but book-change did NOT
    assert cache._last_book_change_ts == t1


def test_note_price_change_real_move_advances(cache):
    """A real best-bid/ask move IS a change → advances _book_changes and the freshness stamp."""
    _run(cache._handle_message(_ticker_msg(_TKR, "0.4500", "0.5500")))
    assert cache._book_changes == 1
    t1 = cache._last_book_change_ts
    _run(cache._handle_message(_ticker_msg(_TKR, "0.4500", "0.5600")))   # yes_ask moved
    assert cache._book_changes == 2
    assert cache._last_book_change_ts >= t1
    _run(cache._handle_message(_ticker_msg(_TKR, "0.4400", "0.5600")))   # yes_bid moved
    assert cache._book_changes == 3
