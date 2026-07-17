"""Kalshi market-shape classification.

The ladder ARB (and its tests) were deleted 2026-07-15 — see bot/kalshi/ladder.py and
docs/TODO.md S5. classify_market survives because macro_pairs uses the same threshold-vs-bucket
shape; that is a Kalshi structural fact, not a ladder concept.
"""
import pytest


def test_classify_threshold_market():
    from bot.kalshi.ladder import classify_market
    assert classify_market({"floor_strike": 60000, "cap_strike": None}) == "threshold"


def test_classify_bucket_market():
    from bot.kalshi.ladder import classify_market
    assert classify_market({"floor_strike": 60000, "cap_strike": 61999.99}) == "bucket"


def test_classify_skip_when_no_floor():
    from bot.kalshi.ladder import classify_market
    assert classify_market({"floor_strike": None, "cap_strike": None}) == "skip"


import asyncio
from unittest.mock import MagicMock


def _evt(event_ticker, markets):
    return {"event_ticker": event_ticker, "markets": markets}


def _mkt(ticker, floor, cap=None, yes_bid=0.5, yes_ask=0.5, status="open"):
    return {
        "ticker": ticker, "floor_strike": floor, "cap_strike": cap,
        "yes_bid_dollars": yes_bid, "yes_ask_dollars": yes_ask, "status": status,
    }


