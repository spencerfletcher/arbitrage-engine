"""Macro cross-platform arb (Fed/CPI): startup validation + detection loop.
Reuses KalshiArbMixin._execute_kalshi_arb for execution. Mixed into BotRunner."""
from __future__ import annotations

import asyncio

from bot.core import config
from bot.core.logger import get_logger
from bot.kalshi.cross_arb import find_macro_arbs
from bot.kalshi.macro_pairs import MACRO_PAIRS, validate_macro_pairs

log = get_logger(__name__)


class MacroMixin:
    async def _validate_macro_pairs_at_startup(self) -> None:
        """One-shot: validate MACRO_PAIRS against live Kalshi markets at startup."""
        if not config.KALSHI_API_KEY or not MACRO_PAIRS:
            if MACRO_PAIRS and not config.KALSHI_API_KEY:
                log.info("Macro arb: MACRO_PAIRS set but KALSHI_API_KEY missing — idle.")
            return
        try:
            raw = await self.kalshi_client.fetch_markets_raw(config.KALSHI_MACRO_SERIES)
        except Exception as e:
            log.error(f"Macro arb startup validation: market fetch failed: {e}")
            return
        live = {m.get("ticker", ""): m for m in raw}
        self._macro_pairs = validate_macro_pairs(MACRO_PAIRS, live)
        if self._macro_pairs:
            log.info(f"Macro arb: {len(self._macro_pairs)}/{len(MACRO_PAIRS)} pairs valid.")
        else:
            log.info("Macro arb: no valid pairs — macro path idle.")

    async def _macro_arb_loop(self) -> None:
        """Detect and execute macro cross-platform arbs (Fed/CPI) every second."""
        if not config.KALSHI_API_KEY:
            return
        while True:
            await asyncio.sleep(1.0)
            if not self._macro_pairs:
                continue
            if not self.kalshi_feed.subscriptions_ready:
                continue
            if self.tracker.has_stranded():
                log.debug("Macro arb paused: stranded position exists")
                continue
            if self._trading_halted():
                continue  # kill switch or daily-loss cap — skip new trades

            _poly_feed_m = self._poly_feed_adapter if self._poly_us else self.feed
            async with self._execution_lock:
                opps = find_macro_arbs(
                    self._macro_pairs, _poly_feed_m, self.kalshi_feed,
                    min_edge=config.KALSHI_MACRO_MIN_EDGE,
                )
                for opp in opps:
                    if self.tracker.is_on_cooldown(opp.poly_event_id):
                        continue
                    current_exposure = self.tracker.total_exposure()
                    if current_exposure + opp.total_cost > config.MAX_TOTAL_EXPOSURE:
                        log.warning(
                            f"Macro arb: exposure cap hit "
                            f"(${current_exposure:.0f}+${opp.total_cost:.0f} "
                            f"> ${config.MAX_TOTAL_EXPOSURE:.0f})"
                        )
                        continue
                    await self._execute_kalshi_arb(opp)
