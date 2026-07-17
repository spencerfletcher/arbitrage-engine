"""
bot/kalshi_scanner.py
─────────────────────
Discovers open Kalshi markets for a set of series tickers.

Mirrors the role of bot/scanner.py for Polymarket — returns a list of
typed KalshiMarket objects ready for cross-platform matching.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bot.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class KalshiMarket:
    ticker: str                      # e.g. "KXNBAGAME-LAKCEL-JUN14"
    event_ticker: str                # e.g. "KXNBAGAME-LAKCEL"
    title: str                       # e.g. "Lakers vs Celtics"
    subtitle: str                    # e.g. "Jun 14, 2026"
    yes_side_label: str              # team/outcome for YES, e.g. "Lakers"
    no_side_label: str               # team/outcome for NO, e.g. "Celtics"
    close_time: str                  # ISO8601 — postponement buffer, NOT game end
    status: str                      # "open", "closed", etc.
    expected_expiration_time: str = ""  # ISO8601 — actual expected game-end + settlement
    yes_bid: float = 0.0             # current YES bid (from REST, for cache priming)
    yes_ask: float = 0.0             # current YES ask (from REST, for cache priming)
    price_tick: float = 0.01         # min price increment (price_ranges[].step; fallback 1¢)


def _parse_price_tick(market: dict) -> float:
    """Min price increment from the market's `price_ranges[0].step` (Kalshi exposes it on the
    nested events market). Fallback 0.01 — every binary market is `linear_cent` today, so this
    is defensive against a future non-cent tick. NEVER 0/None, which would break the floor math
    in kalshi_tick_floor (divide-into-tick)."""
    ranges = market.get("price_ranges") or []
    try:
        step = float(ranges[0].get("step"))
        return step if step > 0 else 0.01
    except (TypeError, ValueError, AttributeError, IndexError, KeyError):
        return 0.01


def _parse_event(event: dict) -> list[KalshiMarket]:
    """Extract open KalshiMarket objects from a Kalshi Events API event dict."""
    event_ticker = event.get("event_ticker", "")
    title = event.get("title", "")
    markets = []
    for m in event.get("markets", []):
        if m.get("status") not in ("open", "active"):
            continue
        markets.append(KalshiMarket(
            ticker=m.get("ticker", ""),
            event_ticker=event_ticker,
            title=title,
            subtitle=m.get("subtitle", ""),
            yes_side_label=m.get("yes_sub_title", ""),
            no_side_label=m.get("no_sub_title", ""),
            close_time=m.get("close_time", ""),
            status=m.get("status", ""),
            expected_expiration_time=(
                m.get("expected_expiration_time") or m.get("occurrence_datetime") or ""
            ),
            yes_bid=float(m.get("yes_bid_dollars") or 0),
            yes_ask=float(m.get("yes_ask_dollars") or 0),
            price_tick=_parse_price_tick(m),
        ))
    return markets


class KalshiScanner:
    """Fetches open Kalshi markets for a list of series tickers."""

    def __init__(self, client) -> None:
        self._client = client
        # Last per-series breakdown logged at INFO. The scanner runs every ~3s, so
        # we re-log only when the mix changes (e.g. MLB 0→14 when games open) —
        # the old unconditional summary line was removed for being too noisy.
        self._last_breakdown: dict[str, int] | None = None

    async def fetch_markets(self, series_tickers: list[str]) -> list[KalshiMarket]:
        """Fetch open markets across the given series tickers."""
        all_markets: list[KalshiMarket] = []
        breakdown: dict[str, int] = {}
        for series in series_tickers:
            try:
                events = await self._client.get_events(series)
                series_markets = [m for event in events for m in _parse_event(event)]
                all_markets.extend(series_markets)
                breakdown[series] = len(series_markets)
                log.debug(f"KalshiScanner: {series} → {len(events)} events")
            except Exception as e:
                log.error(f"KalshiScanner: error fetching {series}: {e}")
        self._log_breakdown(len(all_markets), breakdown)
        return all_markets

    def _log_breakdown(self, total: int, breakdown: dict[str, int]) -> None:
        """Log per-series market counts — INFO when the mix changes, else DEBUG.

        Only successfully-fetched series appear in ``breakdown``; a series that
        errored out is omitted (an error ≠ "0 markets") and so won't flip the mix.
        """
        summary = " ".join(f"{s}={n}" for s, n in breakdown.items())
        msg = (
            f"KalshiScanner: {total} open markets across "
            f"{len(breakdown)} series ({summary})"
        )
        if breakdown != self._last_breakdown:
            log.info(msg)
            self._last_breakdown = breakdown
        else:
            log.debug(msg)
