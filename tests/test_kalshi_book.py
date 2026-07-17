"""Tests for Kalshi orderbook maintenance (orderbook mode) in kalshi_feed."""
import json

import pytest

from bot.kalshi.feed import KalshiOrderBookCache

T = "FED-23DEC-T3.00"


def _cache():
    # series_prefixes=["FED"] so the sample ticker passes the prefix filter.
    return KalshiOrderBookCache(client=None, series_prefixes=["FED"])


def _snapshot(seq=2):
    return json.dumps({
        "type": "orderbook_snapshot", "sid": 2, "seq": seq,
        "msg": {
            "market_ticker": T,
            "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
            "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
        },
    })


def _delta(px, delta, side, seq=3):
    return json.dumps({
        "type": "orderbook_delta", "sid": 2, "seq": seq,
        "msg": {"market_ticker": T, "price_dollars": px, "delta_fp": delta, "side": side},
    })


@pytest.mark.asyncio
async def test_snapshot_builds_book_and_derives_prices():
    c = _cache()
    await c._handle_message(_snapshot())
    # best_yes_bid=0.22, best_no_bid=0.56 → yes_ask=1-0.56=0.44, no_ask=1-0.22=0.78
    assert c.get_best_bid(T, "yes") == pytest.approx(0.22)
    assert c.get_best_ask(T, "yes") == pytest.approx(0.44)
    assert c.get_best_bid(T, "no") == pytest.approx(0.56)
    assert c.get_best_ask(T, "no") == pytest.approx(0.78)
    # depth to BUY yes = size resting on the no bid we'd cross (146); buy no = 333
    assert c.get_depth(T, "yes") == pytest.approx(146.0)
    assert c.get_depth(T, "no") == pytest.approx(333.0)


@pytest.mark.asyncio
async def test_fillable_qty_at_limit():
    # Book: yes bids {0.08:300, 0.22:333}, no bids {0.54:20, 0.56:146}.
    # Buy NO lifts yes bids: a yes bid p is a NO offer at 1-p, takeable when 1-p<=limit
    # i.e. p>=1-limit. Real best NO offer = 1-0.22 = 0.78.
    c = _cache()
    await c._handle_message(_snapshot())
    # limit at the touch (0.78) → only the 0.22 yes bid qualifies → 333
    assert c.fillable_qty(T, "no", 0.78) == pytest.approx(333.0)
    # generous limit (0.92) reaches the 0.08 level too → 300+333 = 633
    assert c.fillable_qty(T, "no", 0.92) == pytest.approx(633.0)
    # limit BELOW the real offer (0.70 < 0.78) → nothing fillable (the phantom case)
    assert c.fillable_qty(T, "no", 0.70) == pytest.approx(0.0)
    # Buy YES lifts no bids: real best YES offer = 1-0.56 = 0.44.
    assert c.fillable_qty(T, "yes", 0.44) == pytest.approx(146.0)
    assert c.fillable_qty(T, "yes", 0.42) == pytest.approx(0.0)


def test_fillable_qty_none_without_book():
    # Ticker mode (no maintained book) → None so callers skip the gate.
    c = _cache()
    assert c.fillable_qty(T, "no", 0.50) is None


@pytest.mark.asyncio
async def test_divergent_from_rest_flags_stale_bid():
    c = _cache()
    c._orderbook_mode = True
    await c._handle_message(_snapshot())          # WS best yes bid = 0.22
    # REST says best yes bid 0.10 → 0.12 gap > tol → flagged (the stale-level bug)
    flagged = c.divergent_from_rest({T: (0.10, 0.50)}, tol=0.03)
    assert any(t[0] == T for t in flagged)
    # WS agrees with REST → not flagged
    assert c.divergent_from_rest({T: (0.22, 0.50)}, tol=0.03) == []


def test_divergent_from_rest_empty_in_ticker_mode():
    c = _cache()  # _orderbook_mode False → integrity check disabled
    assert c.divergent_from_rest({T: (0.50, 0.60)}, tol=0.03) == []


@pytest.mark.asyncio
async def test_resnapshot_noop_when_disconnected():
    c = _cache()
    c._orderbook_mode = True
    await c.resnapshot()  # _ws is None → no error, no-op


@pytest.mark.asyncio
async def test_resnapshot_throttled(monkeypatch):
    from bot.kalshi.feed import config as feed_config
    monkeypatch.setattr(feed_config, "KALSHI_RESNAP_THROTTLE_SECONDS", 30.0)
    c = _cache()
    c._orderbook_mode = True
    assert await c.resnapshot() is True    # first fires
    assert await c.resnapshot() is False   # second within window → throttled
    assert c.book_health()["resnaps"] == 1  # storm prevented


def test_mark_and_is_suspect_per_ticker():
    c = _cache()
    assert c.is_suspect(T) is False
    c.mark_suspect(T, 5.0)
    assert c.is_suspect(T) is True
    assert c.is_suspect("OTHER-TICKER") is False  # only the marked ticker
    # Expired window → no longer suspect.
    c._suspect_until[T] = 0.0
    assert c.is_suspect(T) is False


def test_mark_all_suspect_covers_every_ticker():
    c = _cache()
    c.mark_all_suspect(5.0)
    assert c.is_suspect("ANY-TICKER") is True
    assert c.is_suspect(T) is True


@pytest.mark.asyncio
async def test_seq_gap_marks_all_suspect(monkeypatch):
    from bot.kalshi.feed import config as feed_config
    monkeypatch.setattr(feed_config, "KALSHI_BOOK_SUSPECT_SECONDS", 5.0)
    c = _cache()
    c._orderbook_mode = True
    await c._handle_message(_snapshot(seq=2))
    assert c.is_suspect(T) is False
    await c._handle_message(_delta("0.2200", "5.00", "yes", seq=5))  # gap 2→5
    assert c.is_suspect(T) is True  # gap on the shared stream → all suspect


@pytest.mark.asyncio
async def test_book_health_counts_gaps_and_resnaps():
    c = _cache()
    c._orderbook_mode = True
    await c._handle_message(_snapshot(seq=2))
    h = c.book_health()
    assert h["books"] == 1 and h["seq"] == 2 and h["gaps"] == 0 and h["resnaps"] == 0
    # seq jumps 2 → 5 (missed 3,4) → one gap counted
    await c._handle_message(_delta("0.2200", "5.00", "yes", seq=5))
    assert c.book_health()["gaps"] == 1
    # resnapshot (disconnected → no-op send, but counter still ticks the heal attempt)
    await c.resnapshot()
    assert c.book_health()["resnaps"] == 1


@pytest.mark.asyncio
async def test_delta_removes_top_level_and_redrives():
    c = _cache()
    await c._handle_message(_snapshot())
    # Remove the entire 0.22 yes level → best yes bid drops to 0.08.
    await c._handle_message(_delta("0.2200", "-333.00", "yes", seq=3))
    assert c.get_best_bid(T, "yes") == pytest.approx(0.08)
    assert c.get_best_ask(T, "no") == pytest.approx(0.92)  # 1 - 0.08
    assert c.get_depth(T, "no") == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_delta_adds_and_increments_level():
    c = _cache()
    await c._handle_message(_snapshot())
    # Add a better yes bid at 0.30.
    await c._handle_message(_delta("0.3000", "50.00", "yes", seq=3))
    assert c.get_best_bid(T, "yes") == pytest.approx(0.30)
    assert c.get_depth(T, "no") == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_delta_before_snapshot_is_ignored():
    c = _cache()
    await c._handle_message(_delta("0.2200", "-333.00", "yes"))
    assert c.get_best_bid(T, "yes") is None  # no book yet


def test_global_seq_no_false_gap_across_tickers():
    # Kalshi seq is one global counter across all markets — consecutive global
    # seqs (from different tickers interleaved) must NOT flag a gap.
    c = _cache()
    c._check_seq({"seq": 1})
    c._check_seq({"seq": 2})   # different market, next global seq
    c._check_seq({"seq": 3})
    assert c._seq_global == 3
    assert c._last_gap_action == 0.0  # no gap/resubscribe triggered
