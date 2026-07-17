"""
bot/kalshi_feed.py
──────────────────
Real-time Kalshi price cache via WebSocket. Two sources (KALSHI_PRICE_SOURCE):

  "ticker"    — the `ticker` channel: top-of-book quote for ALL markets, filtered
                to the configured series prefixes. Can lag the executable book.
  "orderbook" — the `orderbook_delta` channel (per matched ticker): maintains the
                real book (snapshot + signed deltas) and derives executable best
                bid/ask + touch depth. Use this to avoid phantom edges.

Either way the cache exposes the same fields, so detection/unwind are agnostic:
  yes_bid, yes_ask, no_bid, no_ask — all floats in [0.0, 1.0]
  no_bid = 1 - yes_ask   (complementary market identity)
  no_ask = 1 - yes_bid
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import websockets

from bot.core import config
from bot.core.feed_health import _stale_book_reconnect
from bot.core.logger import get_logger
from bot.core.ws_timing import ws_timer

log = get_logger(__name__)


@dataclass
class KalshiPriceData:
    yes_bid: float = 0.0
    yes_ask: float = 1.0
    no_bid:  float = 0.0
    no_ask:  float = 1.0
    last_updated: float = field(default_factory=time.time)


class KalshiOrderBookCache:
    """
    Maintains real-time Kalshi prices from the WebSocket `ticker` channel.
    Mirrors the architecture of bot/feed.py (OrderBookCache).
    """

    def __init__(self, client, series_prefixes: list[str] | None = None) -> None:
        """
        Args:
            client:           KalshiClient instance (provides ws_url and ws_headers())
            series_prefixes:  List of ticker prefixes to cache, e.g. ["KXNBAGAME", "KXNHLGAME"].
                              If None, falls back to config.KALSHI_SERIES.
        """
        self._client = client
        self._prefixes = series_prefixes if series_prefixes is not None else config.KALSHI_SERIES
        self._prices: dict[str, KalshiPriceData] = {}
        # Liquidity/activity from the SAME ticker messages (open_interest_fp, volume_fp,
        # last-trade price) — LOGGING-ONLY (phantom-vs-real context), kept in its own dict so the
        # bid/ask price path above is never perturbed. ticker -> (oi, volume, last_trade, ts).
        # Cleared on (re)subscribe so a mode switch (ticker→orderbook, no ticker stream) blanks
        # rather than serving a frozen value. NOTE: only fed in ticker mode (the ticker channel).
        self._liquidity: dict[str, tuple[float | None, float | None, float | None, float]] = {}
        self._on_update_callback: Optional[Callable] = None
        self.subscriptions_ready = False

        # Orderbook mode (KALSHI_PRICE_SOURCE="orderbook"): maintain the real book
        # per ticker and derive executable prices/depth from it (kills the phantom
        # edges the lagging ticker quote produces). _books[ticker][side] = {px: qty}.
        self._orderbook_mode = config.KALSHI_PRICE_SOURCE == "orderbook"
        self._books: dict[str, dict[str, dict[float, float]]] = {}
        self._depth: dict[str, dict[str, float]] = {}   # ticker -> {yes,no} touch depth
        self._book_tickers: list[str] = []               # orderbook subscription set
        # Kalshi seq is a SINGLE monotonic counter across the whole subscription
        # (not per-market) — track it globally; reset to None on (re)subscribe.
        self._seq_global: int | None = None
        self._last_gap_action: float = 0.0               # throttle gap warn/resnapshot
        self._ws = None                                   # live socket (for resubscribe)
        self._last_msg_ts: float = 0.0                    # dead-stream watchdog (ANY message)
        # Data-freshness / frozen-book watchdog — a SEPARATE concern from _last_msg_ts above.
        # Advances ONLY on a real WS top-of-book change (see _note_price_change), so it catches the
        # frozen-resend zombie (ticker frames arriving, tradeable price frozen) that _last_msg_ts
        # (any message — incl. OI/volume-only ticks + library heartbeats) cannot. Mirrors the
        # poly_us feed's _last_book_change_ts. REST writes (prime/refresh) deliberately do NOT touch
        # it — else the 10s REST refresh loop would mask a frozen WS book and defeat the watchdog.
        self._last_book_change_ts: float = 0.0
        self._last_forced_reconnect: float = 0.0          # anti-storm cooldown anchor for the above
        self._book_changes: int = 0                       # cumulative real WS top-of-book changes
        self._book_changes_anchor: int = 0                # book_health() reads the per-interval delta
        # to surface changes/min (the empirical basis for tuning _FRESHNESS_RECONNECT_S post-deploy)
        # Health counters (cumulative, for the periodic ORDERBOOK HEALTH log).
        self._gap_count: int = 0                          # seq gaps detected
        self._resnap_count: int = 0                       # divergence-triggered resnapshots
        # Book-trust gate: a ticker (or all of them, on a global seq gap) is "suspect"
        # — not safe to trade — until its book restabilizes. Set on gap/divergence/
        # resnapshot; read by the execution gate via is_suspect().
        self._suspect_until: dict[str, float] = {}
        self._global_suspect_until: float = 0.0
        self._last_resnap_ts: float = 0.0   # throttle divergence-triggered resnapshots
        # Receive-loop profiling (Phase 3): is the handler keeping up with the stream?
        # Reset each time book_health() reads them, so the health log shows the interval.
        self._msg_count: int = 0
        self._handler_time: float = 0.0     # cumulative seconds inside _handle_message

    # ── Public price access ───────────────────────────────────────────────────

    def set_callback(self, callback: Callable) -> None:
        """Register a callable triggered on every meaningful price update."""
        self._on_update_callback = callback

    def set_book_tickers(self, tickers: list[str]) -> None:
        """Set/refresh the orderbook subscription set (orderbook mode only).

        Resubscribes if the set changed and the socket is connected — mirrors
        poly_us_feed.resubscribe(). No-op outside orderbook mode.
        """
        if not self._orderbook_mode:
            return
        new = sorted(set(tickers))
        if new == self._book_tickers:
            return
        self._book_tickers = new
        if self._ws is not None:
            import asyncio as _a
            _a.create_task(self._resubscribe())

    def get_depth(self, ticker: str, side: str = "yes") -> Optional[float]:
        """Contracts available to BUY `side` at the touch (orderbook mode), or None."""
        d = self._depth.get(ticker)
        if not d:
            return None
        return d.get(side)

    def fillable_qty(self, ticker: str, side: str, limit_price: float) -> Optional[float]:
        """Contracts of `side` buyable at price <= limit_price from the maintained
        book (orderbook mode), or None if no book exists (ticker mode → caller
        skips the gate).

        Buying a side lifts the OPPOSING side's resting bids: a yes bid at price p
        is a NO offer at (1-p), takeable for a NO buy when (1-p) <= limit i.e.
        p >= 1-limit. (Symmetric for a YES buy against no bids.) This is the depth
        actually available AT OUR LIMIT — not just at the touch — so it catches the
        case where the real best offer has moved above our price (0 fillable).
        """
        book = self._books.get(ticker)
        if not book:
            return None
        opp = book.get("yes" if side == "no" else "no", {})
        threshold = round(1.0 - limit_price, 6)
        return sum(qty for px, qty in opp.items() if px >= threshold)

    def get_best_ask(self, ticker: str, side: str = "yes") -> Optional[float]:
        """Return best ask for the given side ("yes" or "no"), or None if unknown."""
        data = self._prices.get(ticker)
        if data is None:
            return None
        return data.yes_ask if side == "yes" else data.no_ask

    def get_best_bid(self, ticker: str, side: str = "yes") -> Optional[float]:
        """Return best bid for the given side ("yes" or "no"), or None if unknown."""
        data = self._prices.get(ticker)
        if data is None:
            return None
        return data.yes_bid if side == "yes" else data.no_bid

    def get_age(self, ticker: str) -> Optional[float]:
        """Return seconds since this ticker's price was last written, or None if unknown."""
        data = self._prices.get(ticker)
        if data is None:
            return None
        return time.time() - data.last_updated

    def get_liquidity(
        self, ticker: str
    ) -> Optional[tuple[Optional[float], Optional[float], Optional[float], float]]:
        """Live (open_interest, volume, last_trade_px, age_s) from the ticker stream, or None if
        this ticker hasn't been seen (incl. orderbook mode, which carries no ticker channel → the
        cache stays empty → blank, never a stale value). LOGGING-ONLY; reads its own dict so it's
        independent of the price path. age_s = now − receive time of the last ticker update; the
        consumer judges staleness from it (OI only moves on trades, so a flat value can be
        genuinely current — stable ≠ frozen — which is why this stamps rather than hard-blanks)."""
        hit = self._liquidity.get(ticker)
        if hit is None:
            return None
        oi, vol, last_px, recv_ts = hit
        return oi, vol, last_px, time.time() - recv_ts

    def prime(self, ticker: str, yes_bid: float, yes_ask: float) -> None:
        """Seed the cache from REST data for a ticker not yet seen on the WS.
        Skips tickers already cached (WS data is more current than REST).
        """
        if ticker in self._prices or yes_ask <= 0:
            return
        import time as _time
        self._prices[ticker] = KalshiPriceData(
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=round(1.0 - yes_ask, 6),
            no_ask=round(1.0 - yes_bid, 6),
            last_updated=_time.time(),
        )

    def refresh(self, ticker: str, yes_bid: float, yes_ask: float) -> None:
        """Update cache from REST data unless the WS has written within 10 seconds.
        Used by the periodic price-refresh loop; WS data is always preferred.
        """
        if self._orderbook_mode:
            return  # orderbook book is the source of truth — don't clobber with
                    # the lagging markets-endpoint quote (the zombie watchdog +
                    # re-snapshot handle a dead book instead).
        if yes_ask <= 0:
            return
        import time as _time
        existing = self._prices.get(ticker)
        if existing and (_time.time() - existing.last_updated) < 10:
            return  # WS wrote recently — don't overwrite with stale REST price
        self._prices[ticker] = KalshiPriceData(
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=round(1.0 - yes_ask, 6),
            no_ask=round(1.0 - yes_bid, 6),
            last_updated=_time.time(),
        )

    # ── Message handling ──────────────────────────────────────────────────────

    def _matches_prefix(self, ticker: str) -> bool:
        return any(ticker.startswith(p) for p in self._prefixes)

    # ── Orderbook maintenance (orderbook mode) ────────────────────────────────

    @staticmethod
    def _levels_to_dict(levels) -> dict[float, float]:
        out: dict[float, float] = {}
        for lvl in levels or []:
            try:
                px, qty = float(lvl[0]), float(lvl[1])
            except (TypeError, ValueError, IndexError):
                continue
            if qty > 0:
                out[round(px, 6)] = qty
        return out

    def _check_seq(self, data: dict) -> None:
        """Track the subscription-wide seq; a real forward gap (missed messages)
        means the book may be stale → throttled warn + resubscribe (resnapshot).
        Resets cleanly on (re)subscribe (seq restarts) — no false alarms."""
        seq = data.get("seq")
        if not isinstance(seq, int):
            return
        last = self._seq_global
        if last is not None and seq > last + 1:
            self._gap_count += 1
            # A dropped message on the shared stream can corrupt ANY book → all suspect.
            self.mark_all_suspect(config.KALSHI_BOOK_SUSPECT_SECONDS)
            now = time.time()
            if now - self._last_gap_action > 15.0:
                self._last_gap_action = now
                log.warning(f"Kalshi book seq gap {last}→{seq} — resubscribing (resnapshot)")
                import asyncio as _a
                _a.create_task(self._resubscribe())
        self._seq_global = seq

    def _note_price_change(self, ticker: str, pd: KalshiPriceData) -> None:
        """Write a ticker's price (ALWAYS — byte-identical to the prior direct assignment) and stamp
        _last_book_change_ts ONLY when the tradeable top-of-book actually moves. Identical re-sends
        and liquidity-only ticks (OI/volume churn at a frozen price) are NOT changes — the
        frozen-resend zombie signature the freshness watchdog must catch. Keys on (yes_bid, yes_ask):
        no_bid/no_ask are exact complements (no_bid = 1−yes_ask, no_ask = 1−yes_bid) in both ticker
        and orderbook mode, so the pair fully determines the book with no info loss and no None risk.
        Mirrors poly_us _note_book. Used by the two WS write paths only (ticker handler + _derive);
        REST writes (prime/refresh) bypass it on purpose (see _last_book_change_ts in __init__)."""
        old = self._prices.get(ticker)
        if old is None or (old.yes_bid, old.yes_ask) != (pd.yes_bid, pd.yes_ask):
            self._book_changes += 1
            self._last_book_change_ts = time.time()
        self._prices[ticker] = pd

    def _derive(self, ticker: str) -> None:
        """Recompute executable prices + touch depth from the maintained book and
        write them into _prices (same shape detection already reads)."""
        book = self._books.get(ticker)
        if not book:
            return
        yes = book.get("yes", {})
        no = book.get("no", {})
        best_yes_bid = max(yes) if yes else 0.0
        best_no_bid = max(no) if no else 0.0
        # To BUY a side you cross the opposing side's best bid: buy-yes hits the no
        # bids (no bid p ⇒ yes offered at 1-p); buy-no hits the yes bids.
        self._note_price_change(ticker, KalshiPriceData(
            yes_bid=best_yes_bid,
            yes_ask=round(1.0 - best_no_bid, 6) if no else 1.0,
            no_bid=best_no_bid,
            no_ask=round(1.0 - best_yes_bid, 6) if yes else 1.0,
            last_updated=time.time(),
        ))
        self._depth[ticker] = {
            "yes": no.get(best_no_bid, 0.0),   # size available to buy YES
            "no": yes.get(best_yes_bid, 0.0),  # size available to buy NO
        }

    async def _handle_message(self, message: str) -> None:
        """Parse a WebSocket message and update the cache. Handles the ticker
        channel (default) and orderbook_snapshot/delta (orderbook mode)."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        self._last_msg_ts = time.time()  # any message proves the socket is alive

        mtype = data.get("type")
        msg = data.get("msg", {})
        ticker = msg.get("market_ticker")
        if not ticker or not self._matches_prefix(ticker):
            return

        if mtype == "orderbook_snapshot":
            self._check_seq(data)
            self._books[ticker] = {
                "yes": self._levels_to_dict(msg.get("yes_dollars_fp")),
                "no": self._levels_to_dict(msg.get("no_dollars_fp")),
            }
            self._derive(ticker)
        elif mtype == "orderbook_delta":
            self._check_seq(data)
            book = self._books.get(ticker)
            if book is None:
                return  # no snapshot yet — wait for it
            try:
                px = round(float(msg["price_dollars"]), 6)
                delta = float(msg["delta_fp"])
                side = msg["side"]
            except (KeyError, TypeError, ValueError):
                return
            levels = book.get(side)
            if levels is None:
                return
            qty = levels.get(px, 0.0) + delta
            if qty > 0:
                levels[px] = qty
            else:
                levels.pop(px, None)
            self._derive(ticker)
        elif mtype == "ticker":
            # Liquidity/activity (LOGGING-ONLY) from the SAME ticker msg, captured FIRST and
            # ISOLATED: before the bid/ask early-return (so a tick missing bid/ask still records
            # liquidity), in its own swallowing try writing only _liquidity (so a malformed
            # liquidity field can NEVER prevent the bid/ask price write below — the load-bearing
            # path). Freshness anchor = receive time (mirrors KalshiPriceData.last_updated).
            try:
                self._liquidity[ticker] = (
                    float(msg["open_interest_fp"]),
                    float(msg["volume_fp"]),
                    float(msg["price_dollars"]),
                    time.time(),
                )
            except (KeyError, TypeError, ValueError):
                pass  # absent/garbled → leave prior (or absent → get_liquidity blank); never raise

            # ── bid/ask price path (UNCHANGED — the fire decision rides on this) ──
            yes_bid_raw = msg.get("yes_bid_dollars")
            yes_ask_raw = msg.get("yes_ask_dollars")
            if yes_bid_raw is None or yes_ask_raw is None:
                return
            yes_bid = float(yes_bid_raw)
            yes_ask = float(yes_ask_raw)
            self._note_price_change(ticker, KalshiPriceData(
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=round(1.0 - yes_ask, 6),
                no_ask=round(1.0 - yes_bid, 6),
                last_updated=time.time(),
            ))
        else:
            return

        if self._on_update_callback:
            self._on_update_callback()

    # ── WebSocket loop ────────────────────────────────────────────────────────

    # Force a reconnect only if NO data frame arrives this long. A genuinely dead
    # socket is already caught by the library's ping_interval=20/ping_timeout=20
    # (closes in ~40s → ConnectionClosed → reconnect), so this guard only exists to
    # catch a silently-dead SUBSCRIPTION (socket alive, server stopped sending).
    # 45s was too aggressive: thin overnight markets go quiet for >45s, tripping a
    # reconnect storm (repeated TLS handshakes → glibc RSS growth → OOM). 180s clears
    # the false positives while still recovering a stuck subscription within 3 min.
    _STALE_RECONNECT_S = 180

    # ── Frozen-book / data-freshness watchdog (the SECOND timer) ────────────────
    # _STALE_RECONNECT_S above = dead STREAM (no message at all). THIS one = frozen BOOK: messages
    # keep arriving (OI/volume ticks, re-sends — so _last_msg_ts stays fresh) while the tradeable
    # top-of-book never moves — the zombie that feeds phantom edges into detection. Keyed on
    # _last_book_change_ts (real WS changes only); see bot/core/feed_health._stale_book_reconnect.
    #
    # THRESHOLD — a PLACEHOLDER, not a derived value (see the measurement note). It is the
    # QUIET-SLATE false-positive floor: it only bites during DEAD periods (few/thin tickers
    # subscribed, between games). During an active game the socket-wide book changes sub-second, so
    # the timer is constantly reset and never approaches this. It is "slow for an in-game freeze" BY
    # DESIGN — a phase-blind socket-wide timer can't be both seconds-fast in-game AND quiet-slate-safe
    # (that needs per-game phase awareness, deferred). Tolerable only because the fire path re-checks
    # fresh REST before firing, so a frozen WS book costs wasted DETECTIONS, not a stale fire.
    #   Why 240, honestly: this is NOT derived — it's the dead-stream timer's 180s + margin. 180s was
    #   calibrated to MESSAGE gaps (45s stormed on thin overnight markets). Book-CHANGE gaps are
    #   strictly LONGER than message gaps (a frozen book still draws OI/heartbeat messages, so changes
    #   are rarer than messages), so a change-threshold should EXCEED 180s, not equal it — 180 would
    #   be too short here and re-court the dead-slate churn (bounded by the 60s cooldown, but
    #   wasteful). 240 is a conservative-ish starting point leaning the right way (up).
    #   PROVISIONAL: replace with a value tuned from the post-deploy dead-slate `changes/min` in
    #   ORDERBOOK HEALTH (the _book_changes hook). Expected direction of any retune: UP, not down.
    _FRESHNESS_RECONNECT_S = 240
    _FRESHNESS_RECONNECT_COOLDOWN_S = 60   # min gap between forced reconnects (anti-storm)

    async def _send_subscribe(self, ws) -> None:
        """Send the channel subscription for the active price source."""
        self._seq_global = None  # new subscription → seq stream restarts
        # Drop cached liquidity on every (re)subscribe: a mode switch (ticker→orderbook, which
        # carries no ticker channel) or any resubscribe must blank get_liquidity rather than serve
        # a value frozen at the last ticker-mode tick — blank-not-stale.
        self._liquidity.clear()
        if self._orderbook_mode:
            params = {"channels": ["orderbook_delta"]}
            if self._book_tickers:
                params["market_tickers"] = self._book_tickers
            await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": params}))
            log.info(f"Kalshi WS: subscribed orderbook_delta ({len(self._book_tickers)} tickers)")
        else:
            await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                      "params": {"channels": ["ticker"]}}))
            log.info("Kalshi WS: subscribed to ticker channel")

    def divergent_from_rest(
        self, rest_map: dict[str, tuple[float, float]], tol: float
    ) -> list[tuple[str, float, float]]:
        """Tickers whose WS-maintained best yes-bid disagrees with the REST book
        by more than `tol` — a sign the delta stream left a stale level behind.

        Returns (ticker, ws_yes_bid, rest_yes_bid) per offender. Orderbook mode
        only ([] otherwise); the caller resnapshots when this is non-empty.
        """
        if not self._orderbook_mode:
            return []
        out: list[tuple[str, float, float]] = []
        for ticker, (rest_yes_bid, _rest_yes_ask) in rest_map.items():
            data = self._prices.get(ticker)
            if data is None:
                continue
            if abs(data.yes_bid - rest_yes_bid) > tol:
                out.append((ticker, data.yes_bid, rest_yes_bid))
        return out

    async def resnapshot(self) -> bool:
        """Force a fresh orderbook snapshot for all book tickers (re-subscribe),
        flushing stale levels the delta stream left behind. THROTTLED: at most once per
        KALSHI_RESNAP_THROTTLE_SECONDS, so a chronically-divergent ticker can't trigger
        a resnapshot storm (it stays suspect via per-ticker marking instead). Returns
        True if it actually resnapshotted. No-op outside orderbook mode."""
        if not self._orderbook_mode:
            return False
        now = time.time()
        if now - self._last_resnap_ts < config.KALSHI_RESNAP_THROTTLE_SECONDS:
            return False
        self._last_resnap_ts = now
        self._resnap_count += 1
        # Book is being rebuilt — don't trade off it until the fresh snapshot lands.
        self.mark_all_suspect(config.KALSHI_BOOK_SUSPECT_SECONDS)
        await self._resubscribe()
        return True

    def mark_suspect(self, ticker: str, seconds: float) -> None:
        """Flag one ticker as not-safe-to-trade for `seconds` (e.g. REST divergence)."""
        self._suspect_until[ticker] = max(
            self._suspect_until.get(ticker, 0.0), time.time() + seconds
        )

    def mark_all_suspect(self, seconds: float) -> None:
        """Flag ALL tickers suspect for `seconds` (e.g. a global seq gap / resnapshot —
        a dropped message can corrupt any book on the shared stream)."""
        self._global_suspect_until = max(self._global_suspect_until, time.time() + seconds)

    def is_suspect(self, ticker: str) -> bool:
        """True if this ticker's book is currently untrustworthy (recent gap/divergence/
        resnapshot). The execution gate refuses to trade a suspect ticker."""
        now = time.time()
        return now < self._global_suspect_until or now < self._suspect_until.get(ticker, 0.0)

    def book_health(self) -> dict:
        """Compact orderbook-maintenance snapshot for the periodic health log. Reads and
        RESETS the receive-loop profiling counters, so each call reports its interval."""
        h = {
            "books": len(self._books),
            "seq": self._seq_global,
            "gaps": self._gap_count,
            "resnaps": self._resnap_count,
            "msgs": self._msg_count,
            "handler_s": self._handler_time,
            "changes": self._book_changes - self._book_changes_anchor,  # real top-of-book moves
        }                                                               # this interval (socket-wide)
        self._msg_count = 0
        self._handler_time = 0.0
        self._book_changes_anchor = self._book_changes
        return h

    async def _resubscribe(self) -> None:
        """Re-send the orderbook subscription after the ticker set changes."""
        if self._ws is None:
            return
        try:
            await self._send_subscribe(self._ws)
        except Exception as e:
            log.warning(f"Kalshi WS: resubscribe failed: {e}")

    async def run_forever(self) -> None:
        """Connect to Kalshi WebSocket and maintain the price cache indefinitely."""
        while True:
            self.subscriptions_ready = False
            self._ws = None
            try:
                ws_url = self._client.ws_url
                log.info(f"Kalshi WS: connecting to {ws_url}")
                async with websockets.connect(
                    ws_url,
                    additional_headers=self._client.ws_headers(),
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    log.info("Kalshi WS: connected")
                    self._ws = ws
                    await self._send_subscribe(ws)
                    self.subscriptions_ready = True
                    self._last_msg_ts = time.time()
                    self._last_book_change_ts = time.time()  # baseline so the freshness watchdog
                    # doesn't fire before the first book change on a freshly-connected socket

                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                            _t0 = time.perf_counter()
                            await self._handle_message(msg)
                            self._handler_time += time.perf_counter() - _t0
                            self._msg_count += 1
                            # Diagnostic only (default off): reuse _t0 (captured before
                            # parse+cache+callback) for the receive-loop timing CSV.
                            if ws_timer.enabled:
                                ws_timer.record_message("kalshi", _t0)
                        except asyncio.TimeoutError:
                            pass  # keepalive handled by ping_interval
                        # Dead-STREAM guard: SDK keeps is_connected True on a dead socket;
                        # force a reconnect if no message at all arrives for a while.
                        if time.time() - self._last_msg_ts > self._STALE_RECONNECT_S:
                            log.warning(
                                f"Kalshi WS: no messages for {self._STALE_RECONNECT_S}s "
                                f"— forcing reconnect"
                            )
                            break
                        # Frozen-BOOK guard: messages arriving but no real top-of-book change —
                        # the zombie the dead-stream check (any message) can't see. Socket-wide,
                        # cooldown-bounded; mirrors the poly_us split.
                        now = time.time()
                        if _stale_book_reconnect(now, self._last_book_change_ts, len(self._prices),
                                                 self._last_forced_reconnect,
                                                 self._FRESHNESS_RECONNECT_S,
                                                 self._FRESHNESS_RECONNECT_COOLDOWN_S):
                            log.warning(
                                f"Kalshi WS: no real book change for {self._FRESHNESS_RECONNECT_S}s "
                                f"across {len(self._prices)} ticker(s) — frozen/zombie, forcing reconnect"
                            )
                            self._last_forced_reconnect = now
                            break

            except websockets.exceptions.ConnectionClosed as e:
                # Routine server cycle (1001) / network drop (1006 no-close-frame) —
                # not a bug, reconnect quietly. ERROR stays for real failures below.
                self.subscriptions_ready = False
                log.info(f"Kalshi WS closed by server ({e}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.subscriptions_ready = False
                log.error(f"Kalshi WS error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
