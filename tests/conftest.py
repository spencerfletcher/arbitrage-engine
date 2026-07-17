"""Shared test guards."""
import pytest

from bot.core import config


@pytest.fixture(autouse=True)
def _no_live_side_effects(monkeypatch):
    """Neutralize live Discord posts in EVERY test.

    A developer's local .env may set DISCORD_WEBHOOK_URL / DISCORD_EDGE_WEBHOOK_URL
    (and DRY_RUN=false). The send_* alert helpers post only when a URL is set, so a
    test that exercises a code path which alerts (e.g. _log_would_fire) would fire a
    real webhook. Blank the URLs for all tests; a test that needs to assert posting
    re-sets them locally (and mocks the webhook transport)."""
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "", raising=False)
    monkeypatch.setattr(config, "DISCORD_EDGE_WEBHOOK_URL", "", raising=False)
