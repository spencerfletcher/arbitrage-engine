"""
tests/test_feed.py
──────────────────
Unit tests for OrderBookCache._handle_message and has_all_prices.

We don't test the live WebSocket loop — that's integration territory.
Instead we feed JSON strings directly into the message handler and
inspect the resulting in-memory cache state.
"""
import asyncio
import json

from bot.polymarket.feed import OrderBookCache, TokenPriceData


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _run(coro):
    """Helper: run an async function in the test."""
    return asyncio.run(coro)


# ── book-event handling ──────────────────────────────────────────────────────

def test_book_event_populates_cache():
    """A 'book' event with asks+bids should populate best_ask and best_bid."""
    cache = OrderBookCache()
    msg = json.dumps({
        "event_type": "book",
        "asset_id": "tok1",
        "asks": [{"price": "0.42", "size": "100"}, {"price": "0.45", "size": "50"}],
        "bids": [{"price": "0.41", "size": "200"}],
    })
    _run(cache._handle_message(msg))

    p = cache.get_price("tok1")
    assert p is not None
    assert p.best_ask == 0.42
    assert p.best_bid == 0.41


def test_book_event_unsorted_levels_picks_true_best():
    """Polymarket returns book levels worst-first (asks high→low, bids low→high).
    best_ask must be the lowest ask and best_bid the highest bid regardless of order.
    """
    cache = OrderBookCache()
    msg = json.dumps({
        "event_type": "book",
        "asset_id": "tok1",
        # Real ordering observed live: asks descending, bids ascending — best at the end.
        "asks": [{"price": "0.99", "size": "100"}, {"price": "0.98", "size": "50"}, {"price": "0.97", "size": "10"}],
        "bids": [{"price": "0.01", "size": "100"}, {"price": "0.02", "size": "50"}, {"price": "0.03", "size": "10"}],
    })
    _run(cache._handle_message(msg))

    p = cache.get_price("tok1")
    assert p is not None
    assert p.best_ask == 0.97   # lowest ask, not asks[0]
    assert p.best_bid == 0.03   # highest bid, not bids[0]


def test_book_event_with_empty_asks_leaves_default():
    """Empty asks list leaves best_ask at the dataclass default (1.0)."""
    cache = OrderBookCache()
    msg = json.dumps({
        "event_type": "book",
        "asset_id": "tok1",
        "asks": [],
        "bids": [{"price": "0.30", "size": "100"}],
    })
    _run(cache._handle_message(msg))

    p = cache.get_price("tok1")
    assert p is not None
    assert p.best_ask == 1.0   # default — no asks were provided
    assert p.best_bid == 0.30


# ── price_change handling ────────────────────────────────────────────────────

def test_price_change_updates_both_fields():
    """A price_change with both best_ask and best_bid updates both."""
    cache = OrderBookCache()
    msg = json.dumps({
        "event_type": "price_change",
        "price_changes": [{
            "asset_id": "tok1",
            "best_ask": "0.55",
            "best_bid": "0.53",
        }],
    })
    _run(cache._handle_message(msg))

    p = cache.get_price("tok1")
    assert p is not None
    assert p.best_ask == 0.55
    assert p.best_bid == 0.53


def test_price_change_with_only_bid_preserves_ask():
    """A price_change with only best_bid leaves best_ask untouched."""
    cache = OrderBookCache()
    cache._prices["tok1"] = TokenPriceData(best_bid=0.40, best_ask=0.60)
    msg = json.dumps({
        "event_type": "price_change",
        "price_changes": [{"asset_id": "tok1", "best_bid": "0.42"}],
    })
    _run(cache._handle_message(msg))

    p = cache.get_price("tok1")
    assert p.best_ask == 0.60   # preserved
    assert p.best_bid == 0.42   # updated


def test_price_change_with_only_ask_preserves_bid():
    """A price_change with only best_ask leaves best_bid untouched."""
    cache = OrderBookCache()
    cache._prices["tok1"] = TokenPriceData(best_bid=0.40, best_ask=0.60)
    msg = json.dumps({
        "event_type": "price_change",
        "price_changes": [{"asset_id": "tok1", "best_ask": "0.58"}],
    })
    _run(cache._handle_message(msg))

    p = cache.get_price("tok1")
    assert p.best_ask == 0.58
    assert p.best_bid == 0.40


def test_unknown_event_type_is_ignored():
    """An unknown event type should not raise or modify the cache."""
    cache = OrderBookCache()
    msg = json.dumps({"event_type": "tick_size_change", "asset_id": "tok1"})
    _run(cache._handle_message(msg))  # should not raise

    assert cache.get_price("tok1") is None


# ── has_all_prices ────────────────────────────────────────────────────────────

def test_has_all_prices_returns_false_for_missing_token():
    cache = OrderBookCache()
    cache._prices["tok1"] = TokenPriceData(best_ask=0.50)
    assert cache.has_all_prices(["tok1", "tok2"]) is False


def test_has_all_prices_treats_default_ask_as_missing():
    """Regression guard: best_ask=1.0 (default) means no real quote yet."""
    cache = OrderBookCache()
    cache._prices["tok1"] = TokenPriceData(best_ask=1.0)   # default
    cache._prices["tok2"] = TokenPriceData(best_ask=0.50)
    assert cache.has_all_prices(["tok1", "tok2"]) is False


def test_has_all_prices_returns_true_when_all_valid():
    cache = OrderBookCache()
    cache._prices["tok1"] = TokenPriceData(best_ask=0.42)
    cache._prices["tok2"] = TokenPriceData(best_ask=0.55)
    assert cache.has_all_prices(["tok1", "tok2"]) is True
