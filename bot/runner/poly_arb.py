"""Polymarket US market discovery + the WS price-update callback. Mixed into BotRunner.

The project's v1 — a same-platform Polymarket + sportsbook arb — is dormant, and its detection/
execution logic was removed from this snapshot (see ARCHITECTURE.md). What remains here is the
live cross-venue path's Poly-US plumbing: discover the market set, and trigger Kalshi cross-arb
detection when a Poly price moves. The cross-arb itself lives in bot/runner/kalshi_arb.py."""
from __future__ import annotations

import asyncio

from bot.core import config
from bot.core.logger import get_logger
from bot.poly_us.sides import parse_token
from bot.runner.common import build_game_windows

log = get_logger(__name__)


class PolyArbMixin:
    def _on_price_update(self):
        """Triggered rapidly by the WebSocket feed when a Poly price changes — a move may open a
        Kalshi cross-arb, so trigger detection at WS speed (it has its own debounce)."""
        self._trigger_kalshi_detect()

    async def _market_discovery_loop(self):
        """Background task that refreshes the Poly US market set every 5 minutes."""
        while True:
            try:
                if self._poly_us:
                    log.info("🔍 Discovering Polymarket US markets for subscription...")
                    pairs = await self._poly_us_scanner.fetch_markets(config.POLYMARKET_US_SERIES)
                    self.active_pairs = pairs
                    # Prime the US feed cache with initial slugs; the WS run_forever loop
                    # subscribes based on keys already present in _prices.
                    live_slugs: set[str] = set()
                    for pair in pairs:
                        for slug in (pair.token_yes_a, pair.token_yes_b):
                            if slug:
                                # Seed with 1.0 (the "no real ask yet" sentinel), NOT a tradeable
                                # mid like 0.5 — priming only populates the WS subscription set. A
                                # real-looking placeholder would generate phantom arbs for any slug
                                # the WS hasn't quoted.
                                self._poly_us_feed.prime(slug, 1.0)
                                live_slugs.add(parse_token(slug)[0])
                    # Prune closed/delisted markets so the tracked set follows the live slate,
                    # instead of accumulating last night's closed games (which never update →
                    # stale prices + an inflated freshness-watchdog count).
                    self._poly_us_feed.retain_slugs(live_slugs)
                    # Hand the freshness watchdog the active-game windows (start/end per pair) so it
                    # only alarms when a game is in-window; off-hours quiet stops looking like a freeze.
                    self._poly_us_feed.set_game_windows(*build_game_windows(pairs))
                    # Pick up newly-discovered games on the live socket without waiting for a
                    # WS reconnect (no-op if not yet connected).
                    await self._poly_us_feed.resubscribe()
                    log.info(f"PolyUSScanner: {len(pairs)} binary markets; US feed primed.")
            except Exception as e:
                log.error(f"Error fetching markets: {e}")
            await asyncio.sleep(300)  # Re-scan for new games every 5 mins
