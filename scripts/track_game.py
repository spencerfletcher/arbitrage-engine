"""
scripts/track_game.py
─────────────────────
Lock in on ONE game: live, continuous edge readout for a single matchup, using the
bot's exact detector (find_kalshi_arbs) over fresh REST snapshots of both venues.

Resolves the matched cross-pair(s) for a keyword (team / event / slug / ticker
substring), then every --interval seconds prints the tightest direction's edge,
prices, and fillable depth — even when negative, so you watch it approach an arb.

Read-only — makes NO trades. Independent of the running bot and of LOG_LEVEL.

Run:
  .venv/bin/python -m scripts.track_game "France"
  .venv/bin/python -m scripts.track_game uzbcol --interval 1
  .venv/bin/python -m scripts.track_game tor-bos
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import time

# Live tool: stream every line immediately (stdout buffers when piped/redirected).
print = functools.partial(print, flush=True)  # noqa: A001

from bot.core import config
from bot.kalshi.cross_arb import find_kalshi_arbs, _effective_share_cost, _kalshi_taker_fee
from bot.poly_us.sides import parse_token

_MATCH = ("event_id", "event_title", "token_yes_a", "token_yes_b")

# Poly best-ask unchanged this long (seconds) while we keep re-polling = a frozen/stale
# read, not a live quote. A 🟢 ARB built on a frozen Poly leg is a phantom: the edge is
# manufactured by the LIVE (Kalshi) leg drifting away from the stuck one, not by a real
# price gap. Flag it loudly so a frozen line can't be mistaken for a capturable arb.
_POLY_FROZEN_S = 10.0


class _PD:
    __slots__ = ("best_ask", "ask_depth", "last_updated")

    def __init__(self, ask: float, depth: float) -> None:
        self.best_ask = ask
        self.ask_depth = depth
        self.last_updated = time.time()


class _PolyShim:
    """get_price(token) over a fresh REST book per tick. long=bestAsk, short=1−bestBid."""
    def __init__(self, client):
        self._c = client
        self._cache: dict[str, tuple[float | None, float]] = {}
        self._state: dict[str, str] = {}   # slug -> MARKET_STATE_*
        # Persists ACROSS refreshes (cache is rebuilt each tick): token -> {ask, changed_at}.
        # Tracks when each best ask last CHANGED value, so we can surface a frozen leg.
        self._fresh: dict[str, dict] = {}

    async def refresh(self, tokens: list[str]) -> None:
        self._cache = {}
        # Fetch all slugs concurrently so the snapshot is as close to one instant as
        # possible (sequential fetches let prices drift between legs → false edges).
        slugs = list({parse_token(t)[0] for t in tokens})
        results = await asyncio.gather(*(self._c.get_book(s) for s in slugs),
                                       return_exceptions=True)
        books = {s: (r if not isinstance(r, Exception) else None)
                 for s, r in zip(slugs, results)}
        for s in slugs:
            md = (books.get(s) or {}).get("marketData", {}) if isinstance(books.get(s), dict) else {}
            self._state[s] = md.get("state", "?")
        now = time.time()
        for t in tokens:
            slug, is_short = parse_token(t)
            md = (books.get(slug) or {}).get("marketData", {}) if isinstance(books.get(slug), dict) else {}
            levels = md.get("bids", []) if is_short else md.get("offers", [])
            pxs = [(float(l["px"]["value"]), float(l["qty"])) for l in (levels or []) if l.get("px")]
            if not pxs:
                self._cache[t] = (None, 0.0)
                continue
            best = max(p for p, _ in pxs) if is_short else min(p for p, _ in pxs)
            depth = sum(q for p, q in pxs if p == best)
            ask = round(1.0 - best, 6) if is_short else best
            self._cache[t] = (ask, depth)
            # Reset the freshness clock only when the best ask actually moves.
            prev = self._fresh.get(t)
            if prev is None or prev["ask"] != ask:
                self._fresh[t] = {"ask": ask, "changed_at": now}

    def get_price(self, token: str):
        ask, depth = self._cache.get(token, (None, 0.0))
        if ask is None or ask >= 1.0:
            return None
        return _PD(ask, depth)

    def state(self, token: str) -> str:
        return self._state.get(parse_token(token)[0], "?")

    def age(self, token: str) -> float | None:
        """Seconds since this token's best ask last CHANGED value — freshness, NOT
        time-since-fetch. A large age while the other leg keeps moving = a frozen/stale
        Poly read. None if we've never priced it."""
        f = self._fresh.get(token)
        return (time.time() - f["changed_at"]) if f else None


class _KalshiShim:
    """get_best_ask(ticker, side) over a fresh REST orderbook per tick. Also exposes
    fillable() for the display. ask(side) = 1 − best bid of the opposite side."""
    def __init__(self, client):
        self._c = client
        self._cache: dict[str, dict | None] = {}

    async def refresh(self, tickers: list[str]) -> None:
        self._cache = {}
        results = await asyncio.gather(*(self._c.get_orderbook(tk, depth=20) for tk in tickers),
                                       return_exceptions=True)
        self._cache = {tk: (r if not isinstance(r, Exception) else None)
                       for tk, r in zip(tickers, results)}

    def _levels(self, ticker: str, key: str):
        book = self._cache.get(ticker)
        ob = (book or {}).get("orderbook_fp", {}) if isinstance(book, dict) else {}
        return [(float(l[0]), float(l[1])) for l in (ob.get(key) or []) if l]

    def get_best_ask(self, ticker: str, side: str):
        opp = "no_dollars" if side == "yes" else "yes_dollars"
        pxs = [p for p, _ in self._levels(ticker, opp)]
        return round(1.0 - max(pxs), 6) if pxs else None

    def fillable(self, ticker: str, side: str, limit: float) -> float:
        # Contracts of `side` buyable at <= limit (mirrors runner._rest_fillable).
        opp = "yes_dollars" if side == "no" else "no_dollars"
        thr = round(1.0 - limit, 6)
        return sum(q for p, q in self._levels(ticker, opp) if p >= thr)

    def ask_depth(self, ticker: str, side: str) -> float:
        # Contracts available AT the current best ask for `side` (the size at the
        # quoted price, not the whole book). best ask = 1 − best opposite-side bid;
        # its depth = qty resting at that top opposite-side bid level.
        opp = "no_dollars" if side == "yes" else "yes_dollars"
        levels = self._levels(ticker, opp)
        if not levels:
            return 0.0
        best = max(p for p, _ in levels)
        return sum(q for p, q in levels if p == best)


async def _resolve(sdk, kalshi, keyword: str):
    from bot.poly_us.scanner import PolyUSScanner
    from bot.kalshi.scanner import KalshiScanner
    from bot.kalshi.matcher import match_kalshi_events, is_settlement_equivalent
    poly_pairs = await PolyUSScanner(sdk).fetch_markets(config.POLYMARKET_US_SERIES)
    kmarkets = []
    for sid in config.KALSHI_SERIES:
        kmarkets.extend(await KalshiScanner(kalshi).fetch_markets([sid]))
    matched = match_kalshi_events(poly_pairs, kmarkets, {})
    # Only track settlement-equivalent pairs — the same filter the bot applies, so a
    # 🟢 ARB here corresponds to a pair the bot would actually consider hedgeable.
    matched = [cp for cp in matched
               if is_settlement_equivalent(cp.kalshi_market.ticker, cp.poly_pair.settlement_type)]
    kw = keyword.lower()

    def hit(cp):
        p = cp.poly_pair
        return (kw in (p.event_title or "").lower() or kw in (p.event_id or "").lower()
                or kw in (p.token_yes_a or "").lower() or kw in cp.kalshi_market.ticker.lower())
    return [cp for cp in matched if hit(cp)]


async def main() -> None:
    ap = argparse.ArgumentParser(description="Live edge tracker for one game")
    ap.add_argument("keyword", help="team / event / slug / ticker substring")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    args = ap.parse_args()

    from bot.poly_us.client import PolyUSClient
    from bot.kalshi.client import KalshiClient
    from polymarket_us import AsyncPolymarketUS

    sdk = AsyncPolymarketUS(key_id=config.POLYMARKET_US_KEY_ID or None,
                            secret_key=config.POLYMARKET_US_SECRET_KEY or None)
    poly_client = PolyUSClient()
    kalshi = KalshiClient()
    poly_shim, kalshi_shim = _PolyShim(poly_client), _KalshiShim(kalshi)
    try:
        pairs = await _resolve(sdk, kalshi, args.keyword)
        if not pairs:
            print(f"No matched cross-pair for '{args.keyword}'. Try `scripts.list_markets`.")
            return
        title = pairs[0].poly_pair.event_title
        tickers = sorted({cp.kalshi_market.ticker for cp in pairs})
        tokens = sorted({t for cp in pairs for t in (cp.poly_pair.token_yes_a, cp.poly_pair.token_yes_b)})
        print(f"🔒 Tracking: {title}  ({len(pairs)} cross-pair(s), {len(tickers)} Kalshi ticker(s))")
        print("   poll every %.1fs — Ctrl-C to stop\n" % args.interval)

        while True:
            # Refresh BOTH venues concurrently → Poly and Kalshi snapshots land in the
            # same ~instant, so a 🟢 ARB reflects a genuinely simultaneous edge (not a
            # skew between sequential reads).
            await asyncio.gather(poly_shim.refresh(tokens), kalshi_shim.refresh(tickers))
            opps = find_kalshi_arbs(pairs, poly_shim, kalshi_shim, min_edge=-1.0)
            ts = time.strftime("%H:%M:%S")
            if not opps:
                # Show WHY a tick couldn't price an edge, per leg — distinguishes a Poly
                # suspension / one-sided book from a missing Kalshi quote.
                bits = []
                for cp in pairs:
                    pp = cp.poly_pair
                    la = poly_shim.get_price(pp.token_yes_a)
                    sa = poly_shim.get_price(pp.token_yes_b)
                    st = poly_shim.state(pp.token_yes_a).replace("MARKET_STATE_", "")
                    ky = kalshi_shim.get_best_ask(cp.kalshi_market.ticker, "yes")
                    kn = kalshi_shim.get_best_ask(cp.kalshi_market.ticker, "no")
                    bits.append(
                        f"poly[{st}] long={la.best_ask if la else None} short={sa.best_ask if sa else None}"
                        f" | kalshi yes={ky} no={kn}")
                print(f"{ts}  no edge — " + "  ||  ".join(bits))
            for o in opps:
                fillable = kalshi_shim.fillable(o.kalshi_ticker, o.kalshi_side,
                                                round(1.0 - o.poly_ask, 4))
                kdepth = kalshi_shim.ask_depth(o.kalshi_ticker, o.kalshi_side)
                combined = _effective_share_cost(o.poly_ask_raw, 0.05) + o.kalshi_ask + _kalshi_taker_fee(o.kalshi_ask)
                pstate = poly_shim.state(o.poly_token)
                open_ = pstate == "MARKET_STATE_OPEN"
                # Poly's OWN depth at the quoted ask — the binding leg (kdepth/fillable
                # below are KALSHI-only). A fat kalshi `fillable` means nothing if the
                # Poly leg is thin or frozen.
                _pd = poly_shim.get_price(o.poly_token)
                poly_depth = _pd.ask_depth if _pd else 0.0
                poly_age = poly_shim.age(o.poly_token)
                _agestr = f"{poly_age:.0f}s" if poly_age is not None else "?"
                frozen = poly_age is not None and poly_age >= _POLY_FROZEN_S
                # A positive edge while Poly is NOT open = a halt/suspension phantom:
                # the Poly price is frozen (untradeable), not a real arb.
                if o.edge > 0:
                    flag = "🟢 ARB " if open_ else "⛔HALT "
                else:
                    flag = "  ···  "
                pstag = "" if open_ else f"  ⚠️poly={pstate.replace('MARKET_STATE_', '')}"
                # A 🟢/⛔ edge on a frozen Poly leg is a phantom — the edge is just the
                # live Kalshi leg drifting off a stuck Poly quote. Flag it unmissably.
                ftag = f"  ⚠️FROZEN(poly {_agestr})" if frozen else ""
                print(f"{ts} {flag} {o.edge*100:+6.2f}%  "
                      f"poly {o.poly_team[:14]:14}@{o.poly_ask_raw:.3f}(d={poly_depth:.0f} age={_agestr})  "
                      f"kalshi {o.kalshi_side}@{o.kalshi_ask:.3f} (depth {kdepth:.0f})  "
                      f"combined {combined:.4f}  fillable={fillable:.0f}{pstag}{ftag}")
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        await sdk.close()
        await poly_client.close()
        await kalshi.close()


if __name__ == "__main__":
    asyncio.run(main())
