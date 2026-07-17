"""Tests for the raw Polymarket US markets WebSocket transport (bot/poly_us/feed.py).

Covers the pure pieces that don't need a live socket: Ed25519 auth header
construction (signature must verify against the documented message), the subscribe
payload shape, and message dispatch by top-level key. The connect/recv loop itself
is validated live via scripts/poly_us_ws_capture.py.
"""
import base64
import time

from nacl.signing import SigningKey, VerifyKey

import bot.poly_us.feed as feed_mod
from bot.poly_us.feed import PolyUSOrderBookCache, _WS_PATH


def _cache():
    return PolyUSOrderBookCache(sdk=None)


# ── Ed25519 auth headers ────────────────────────────────────────────────────

def test_auth_headers_signature_verifies(monkeypatch):
    sk = SigningKey.generate()
    seed_b64 = base64.b64encode(bytes(sk)).decode()  # bytes(sk) == 32-byte seed
    monkeypatch.setattr(feed_mod.config, "POLYMARKET_US_KEY_ID", "kid-123")
    monkeypatch.setattr(feed_mod.config, "POLYMARKET_US_SECRET_KEY", seed_b64)

    h = _cache()._auth_headers()
    assert h["X-PM-Access-Key"] == "kid-123"
    # Message the server reconstructs: timestamp + "GET" + path
    message = f'{h["X-PM-Timestamp"]}GET{_WS_PATH}'
    # Raises BadSignatureError if the signature doesn't match → test fails.
    sk.verify_key.verify(message.encode(), base64.b64decode(h["X-PM-Signature"]))


def test_auth_headers_accepts_64_byte_key(monkeypatch):
    sk = SigningKey.generate()
    full = bytes(sk) + bytes(sk.verify_key)  # 64-byte seed||pubkey form
    monkeypatch.setattr(feed_mod.config, "POLYMARKET_US_KEY_ID", "kid")
    monkeypatch.setattr(feed_mod.config, "POLYMARKET_US_SECRET_KEY", base64.b64encode(full).decode())

    h = _cache()._auth_headers()
    message = f'{h["X-PM-Timestamp"]}GET{_WS_PATH}'
    sk.verify_key.verify(message.encode(), base64.b64decode(h["X-PM-Signature"]))


# ── Subscribe payload ───────────────────────────────────────────────────────

def test_subscribe_payload_shape():
    p = _cache()._subscribe_payload(["slug-a", "slug-b"])
    sub = p["subscribe"]
    assert sub["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"
    assert sub["marketSlugs"] == ["slug-a", "slug-b"]
    assert isinstance(sub["requestId"], str) and sub["requestId"]


# ── Dispatch by top-level key ───────────────────────────────────────────────

def test_dispatch_market_data_sets_ask_and_depth():
    c = _cache()
    c._dispatch({"marketData": {"marketSlug": "S", "offers": [
        {"px": {"value": "0.41"}, "qty": "50"},
        {"px": {"value": "0.40"}, "qty": "100"},   # best ask = lowest
        {"px": {"value": "0.40"}, "qty": "25"},    # same level adds to depth
    ]}})
    assert c.get_best_ask("S") == 0.40
    assert c.get_depth("S") == 125.0


def test_dispatch_market_data_lite_sets_ask_no_depth():
    c = _cache()
    c._dispatch({"marketDataLite": {"marketSlug": "S", "bestAsk": {"value": "0.55"}}})
    assert c.get_best_ask("S") == 0.55
    assert c.get_depth("S") == 0.0


def test_dispatch_heartbeat_bumps_liveness_without_price():
    c = _cache()
    c._last_msg_ts = 0.0
    c._dispatch({"heartbeat": {}})
    assert c._last_msg_ts > 0.0
    assert c.get_best_ask("S") is None


def test_dispatch_error_and_unknown_are_safe():
    c = _cache()
    # Neither should raise.
    c._dispatch({"error": "bad subscription", "requestId": "r1"})
    c._dispatch({"somethingNew": {"x": 1}})
    assert c.get_best_ask("S") is None
