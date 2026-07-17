"""
bot/main.py
───────────
Entry point for the Polymarket sports arbitrage bot.

  Usage:
    python -m bot.main            # continuous loop
    python -m bot.main --once     # single scan then exit (for testing)

The orchestrator itself lives in bot/runner/ (BotRunner + per-concern mixins);
this file is just the CLI banner + launch.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

# Force Windows terminals to support utf-8 emojis in our print banners
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from bot.core import config
from bot.core.logger import get_logger
from bot.kalshi.macro_pairs import MACRO_PAIRS
from bot.runner import BotRunner

log = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Sports Arbitrage Bot")
    parser.add_argument("--once", action="store_true", help="Run a single scan then exit (useful for testing)")
    args = parser.parse_args()

    mode_tag = "🔍 DRY RUN" if config.DRY_RUN else "⚡ LIVE TRADING"
    print(f"\n{'='*60}")
    print(f"  Kalshi ↔ Polymarket US cross-arb [{mode_tag}]")
    print(f"  Min edge  : {config.KALSHI_ARB_MIN_EDGE:.1%} NET (both legs' taker fees included)")
    print(f"  Max/leg   : ${config.MAX_POSITION_USD:.0f}")
    print(f"  Max total : ${config.MAX_TOTAL_EXPOSURE:.0f}")
    print(f"  Cooldown  : {config.EVENT_COOLDOWN_SECONDS}s")
    print(f"  Log level : {config.LOG_LEVEL}")
    pushover = "✅" if config.PUSHOVER_USER_KEY else "disabled"
    print(f"  Pushover  : {pushover}")
    kalshi_status = "✅" if config.KALSHI_API_KEY else "disabled (no KALSHI_API_KEY)"
    print(f"  Kalshi    : {kalshi_status} ({config.KALSHI_ENV})")
    if config.KALSHI_API_KEY:
        from bot.kalshi.client import _PROD_WS, _DEMO_WS
        _kalshi_ws = _PROD_WS if config.KALSHI_ENV == "prod" else _DEMO_WS
        print(f"  Kalshi WS : {_kalshi_ws}")
        print(f"  Kalshi ser: {', '.join(config.KALSHI_SERIES)}")
        print(f"  Kalshi min: edge={config.KALSHI_ARB_MIN_EDGE}")
        # The book-trust gate (feed.is_suspect: seq-gap / REST-divergence) only exists in
        # orderbook mode — in ticker mode no orderbook channel is subscribed, so _check_seq never
        # runs and is_suspect() is a constant False. Say so at startup: architecture.md listed it as
        # a live pipeline gate, and a safety net that reads as armed but cannot fire is worse than
        # none. See docs/TODO.md S6.
        print(f"  Kalshi px : {config.KALSHI_PRICE_SOURCE}"
              + ("  ⚠️ book-trust gate INACTIVE (ticker mode: is_suspect can never fire; "
                 "fresh-REST re-read is the real guard)"
                 if config.KALSHI_PRICE_SOURCE != "orderbook" else ""))
        print(f"  Kalshi mac: {', '.join(config.KALSHI_MACRO_SERIES)}  min_edge={config.KALSHI_MACRO_MIN_EDGE}  pairs={len(MACRO_PAIRS)}")
    print(f"{'='*60}\n")

    if not config.DRY_RUN:
        log.warning("🔴 LIVE TRADING MODE. Press Ctrl-C within 5 seconds to abort.")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            sys.exit(0)

    runner = BotRunner()

    # Show existing exposure
    exposure = runner.tracker.total_exposure()
    if exposure > 0:
        positions = runner.tracker.list_positions()
        log.info(f"📊 Open exposure: ${exposure:.0f} across {len(positions)} position(s)")

    try:
        asyncio.run(runner.start())
    except KeyboardInterrupt:
        log.info("Interrupted by user. Shutting down gracefully.")


if __name__ == "__main__":
    main()
