"""Tests for the 'proper edge' Discord alert (dedicated channel)."""
import types
from unittest.mock import MagicMock
import pytest

import bot.core.alerts as alerts
from bot.core import config


def _opp():
    return types.SimpleNamespace(
        event_title="Toronto Blue Jays vs. Boston Red Sox",
        edge=0.035, guaranteed_profit=1.06, shares=9,
        poly_team="Toronto Blue Jays", poly_ask=0.52, poly_token="aec-mlb-tor-bos",
        kalshi_team="Toronto", kalshi_side="no", kalshi_ask=0.33,
        kalshi_ticker="KXMLBGAME-26JUN18TORBOS-TOR",
    )


class _FakeWebhook:
    instances = []
    def __init__(self, url=None, content="", timeout=None):
        self.url = url
        self.timeout = timeout      # None here would mean "wait forever" in the real library
        self.executed = False
        _FakeWebhook.instances.append(self)
    def add_embed(self, e): pass
    def execute(self): self.executed = True


def _patch(monkeypatch, edge_url, main_url=""):
    _FakeWebhook.instances = []
    monkeypatch.setattr(alerts, "DiscordWebhook", _FakeWebhook)
    monkeypatch.setattr(alerts, "DiscordEmbed", lambda **k: MagicMock())
    monkeypatch.setattr(config, "DISCORD_EDGE_WEBHOOK_URL", edge_url)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", main_url)


def test_posts_to_edge_webhook(monkeypatch):
    _patch(monkeypatch, edge_url="https://discord/edge")
    alerts.send_proper_edge_alert(_opp(), poly_depth=400, kalshi_fillable=500, is_dry_run=True)
    assert len(_FakeWebhook.instances) == 1
    assert _FakeWebhook.instances[0].url == "https://discord/edge"
    assert _FakeWebhook.instances[0].executed is True


def test_falls_back_to_main_webhook(monkeypatch):
    _patch(monkeypatch, edge_url="", main_url="https://discord/main")
    alerts.send_proper_edge_alert(_opp(), poly_depth=400, kalshi_fillable=500)
    assert _FakeWebhook.instances[0].url == "https://discord/main"


def test_noop_when_no_webhook_configured(monkeypatch):
    _patch(monkeypatch, edge_url="", main_url="")
    alerts.send_proper_edge_alert(_opp(), poly_depth=400, kalshi_fillable=500)
    assert _FakeWebhook.instances == []   # nothing constructed → no post


# ── the freeze ────────────────────────────────────────────────────────────────────────────────
# DiscordWebhook defaults timeout to None and requests reads None as "wait forever". One
# un-timed-out post on the event loop hangs the whole bot: both feeds stop reading, the websockets
# library can't answer server pings from the same loop so the sockets drop, and systemd sees
# `active (running)` forever (Restart=always, no WatchdogSec). Proven 2026-07-16 against a
# black-hole socket. These pin both halves of the fix.

def test_every_webhook_carries_a_timeout(monkeypatch):
    """A missing timeout is not a slow post — it is an indefinite hang."""
    from bot.core.alerts import _DISCORD_TIMEOUT_S
    _patch(monkeypatch, edge_url="https://discord/edge")
    alerts.send_proper_edge_alert(_opp(), poly_depth=400, kalshi_fillable=500, is_dry_run=True)
    assert _FakeWebhook.instances[0].timeout == _DISCORD_TIMEOUT_S
    assert _DISCORD_TIMEOUT_S is not None and _DISCORD_TIMEOUT_S > 0


@pytest.mark.asyncio
async def test_dispatch_does_not_block_the_event_loop(monkeypatch):
    """The timeout bounds the hang; this keeps it off the loop entirely.

    10s of stall is still fatal on a path whose budget is ~100ms — and four of these fire from
    the exec helpers while _execution_lock is held, i.e. potentially between a Poly fill and the
    Kalshi hedge. A post must never cost the loop anything.
    """
    import asyncio, time

    class _SlowWebhook(_FakeWebhook):
        def execute(self):
            time.sleep(0.5)          # a blocking post, as `requests` really is
            self.executed = True

    monkeypatch.setattr(alerts, "DiscordWebhook", _SlowWebhook)
    monkeypatch.setattr(alerts, "DiscordEmbed", lambda **k: MagicMock())
    monkeypatch.setattr(config, "DISCORD_EDGE_WEBHOOK_URL", "https://discord/edge")
    _FakeWebhook.instances = []

    t0 = time.perf_counter()
    alerts.send_proper_edge_alert(_opp(), poly_depth=400, kalshi_fillable=500, is_dry_run=True)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1, f"the send blocked the loop for {elapsed:.2f}s — it must not block at all"

    # ...and it really does post, on its own thread.
    for _ in range(40):
        await asyncio.sleep(0.05)
        if _FakeWebhook.instances and _FakeWebhook.instances[0].executed:
            break
    assert _FakeWebhook.instances[0].executed is True


def test_dispatch_posts_inline_when_there_is_no_loop(monkeypatch):
    """Off the loop already (a to_thread worker, a script, a sync test) → just post.
    send_proper_edge_alert is itself called via asyncio.to_thread from the fire path."""
    _patch(monkeypatch, edge_url="https://discord/edge")
    alerts.send_proper_edge_alert(_opp(), poly_depth=400, kalshi_fillable=500, is_dry_run=True)
    assert _FakeWebhook.instances[0].executed is True     # synchronously, no flush needed
