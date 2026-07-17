"""Hourly combined-balance snapshot: refreshes sizing caps + records the peak for
FBAR/tax tracking. Mixed into BotRunner."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from bot.core import config
from bot.core.logger import get_logger

log = get_logger(__name__)


def _rss_mb() -> float | None:
    """Resident set size (MB) from /proc/self/status, or None if unavailable.

    Linux-only (no-ops on dev macOS). Dependency-free — reads VmRSS directly."""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0  # kB → MB
    except OSError:
        return None
    return None


class BalanceMixin:
    async def _memory_watch_loop(self) -> None:
        """Log process RSS every 2 min so the memory curve is visible (leak vs
        plateau). The box is small (≈900MB, no swap) and has OOM-killed; this makes
        the growth rate diagnosable from the logs without remote profiling."""
        while True:
            rss = _rss_mb()
            if rss is not None:
                log.debug(f"mem: RSS={rss:.0f}MB")
            await asyncio.sleep(120)

    async def _peak_balance_loop(self) -> None:
        """Hourly: record combined Poly+Kalshi balance peak for FBAR/tax tracking."""
        _PEAK_LOG = Path("logs/peak_balance.log")
        _FBAR_WARN_USD = 8_000.0
        _FBAR_THRESHOLD_USD = 10_000.0

        first_run = True
        while True:
            if not first_run:
                await asyncio.sleep(3600)
            first_run = False
            try:
                # force_real=True: record the ACTUAL balance for FBAR/tax tracking,
                # not the DRY_RUN 9999 sentinel (balance fetch is read-only/safe).
                if self._poly_us:
                    poly_bal = await self.client.get_usdc_balance(force_real=True)
                else:
                    poly_bal = await asyncio.to_thread(self.client.get_usdc_balance, True)
                kalshi_bal = await self.kalshi_client.get_balance() if config.KALSHI_API_KEY else 0.0
                # Refresh the sizing caps with real balances (ignore -1 error sentinel).
                if poly_bal is not None and poly_bal >= 0:
                    self._poly_buying_power = poly_bal
                if kalshi_bal is not None and kalshi_bal >= 0:
                    self._kalshi_buying_power = kalshi_bal
                total      = (poly_bal or 0.0) + (kalshi_bal or 0.0)
                utc_date   = time.strftime("%Y-%m-%d", time.gmtime())
                record = {
                    "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "date_utc":    utc_date,
                    "poly_usd":    round(poly_bal or 0.0, 2),
                    "kalshi_usd":  round(kalshi_bal or 0.0, 2),
                    "total_usd":   round(total, 2),
                }
                _PEAK_LOG.parent.mkdir(parents=True, exist_ok=True)
                with _PEAK_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")

                # Balances are real (force_real above), so FBAR checks apply in any
                # mode — the obligation is about real money held, not bot mode.
                if total >= _FBAR_THRESHOLD_USD:
                    log.warning(
                        f"FBAR THRESHOLD REACHED: combined balance ${total:.2f} >= ${_FBAR_THRESHOLD_USD:.0f}. "
                        f"FinCEN 114 filing required for {utc_date[:4]}."
                    )
                elif total >= _FBAR_WARN_USD:
                    log.warning(
                        f"FBAR WARNING: combined balance ${total:.2f} approaching "
                        f"${_FBAR_THRESHOLD_USD:.0f} reporting threshold."
                    )
                else:
                    log.info(f"Balance snapshot: Poly=${poly_bal:.2f} Kalshi=${kalshi_bal:.2f} total=${total:.2f}")
            except Exception as e:
                log.error(f"Peak balance check failed: {e}")
