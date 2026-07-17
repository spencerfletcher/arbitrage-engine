"""
bot/poly_us_client.py
─────────────────────
Authenticated Polymarket US client (polymarket-us SDK) with dry-run support.

Polymarket US is a separate, CFTC-regulated exchange from global Polymarket:
  - API-key auth (key_id + secret_key), NOT wallet/EIP-712 signing
  - Custodial balances (no Polygon wallet)
  - Markets identified by slug (e.g. "atc-fwc-mex-rsa-2026-06-11-mex"), not token IDs

This client presents the same surface the rest of the bot expects from
PolymarketClient (get_usdc_balance / get_best_ask / place_limit_fok), so the
matcher, arb math, and executor can treat either venue uniformly. The opaque
"market_slug" string carried in MarketPair.token_* fields is interpreted here.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from polymarket_us import AsyncPolymarketUS

from bot.core import config
from bot.core.logger import get_logger
from bot.poly_us.sides import parse_token

log = get_logger(__name__)


def _amount_to_float(amount: Optional[dict]) -> Optional[float]:
    """Parse a polymarket-us Amount ({'value': str, 'currency': 'USD'}) to float."""
    if not amount:
        return None
    try:
        return float(amount["value"])
    except (KeyError, TypeError, ValueError):
        return None


# Phantom-vs-real liquidity/activity from a book's marketData.stats — None for each absent field.
_EMPTY_BOOK_STATS: dict = {
    "open_interest": None, "oi_age_s": None, "last_trade_px": None, "last_trade_qty": None,
    "last_trade_age_s": None, "shares_traded": None, "notional_traded": None,
}


def _parse_book_stats(md: Optional[dict], now: float) -> dict:
    """Liquidity/activity from a Poly book's `marketData.stats` — FREE (same fetch as the quote),
    LOGGING-ONLY. Every field is optional → None when absent; this MUST NOT raise (it rides the
    fire-path read, so a malformed stats block can never break the quote). `*SetTime` ages reuse
    `transact_age_s` (same ns-ISO format as transactTime). `openInterest`/`sharesTraded` are bare
    numeric strings; `lastTradePx`/`notionalTraded` are {value,currency} Amounts."""
    s = md.get("stats") if isinstance(md, dict) else None
    if not isinstance(s, dict):
        return dict(_EMPTY_BOOK_STATS)

    def _num(x) -> Optional[float]:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    return {
        "open_interest": _num(s.get("openInterest")),
        "oi_age_s": transact_age_s(s.get("openInterestSetTime"), now),
        "last_trade_px": _amount_to_float(s.get("lastTradePx")),
        "last_trade_qty": _num(s.get("lastTradeQty")),
        "last_trade_age_s": transact_age_s(s.get("lastTradeSetTime"), now),
        "shares_traded": _num(s.get("sharesTraded")),
        "notional_traded": _amount_to_float(s.get("notionalTraded")),
    }


def transact_age_s(transact_time: Optional[str], now: float) -> Optional[float]:
    """Seconds between `now` (epoch) and a Polymarket book transactTime (ISO-8601, ns
    precision). None on missing/unparseable input — fail-open, so a logging field can never
    break the fire path. Truncates the fraction to microseconds (datetime.fromisoformat
    rejects 9 fractional digits) and strips a trailing 'Z'."""
    if not transact_time:
        return None
    s = transact_time.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    try:
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return now - dt.timestamp()
    except ValueError:
        return None


def quote_from_md(
    md: dict, is_short: bool
) -> tuple[Optional[float], str, list[tuple[float, float]], Optional[str], dict]:
    """The ask-space read of a book's `marketData`. See PolyUSClient.get_fill_quote, which is a
    fetch wrapped around this, for what every returned value means.

    Split out of it so a caller ALREADY HOLDING a book can read the same quote without fetching
    again. That matters on Poly specifically: the limit is ~1 req/s sustained and over-limit is
    THROTTLED rather than rejected — a late, stale 200. A second fetch for data already in hand
    therefore doesn't just waste a request, it degrades the freshness of the reads around it, and
    the fire path shares that budget. A caller wanting both directions of one book reads the ask
    here and the bid via kalshi_arb._poly_exit_from_book, off a single fetch — which also makes
    the two describe one instant instead of two books a round-trip apart.

    No I/O, and no state — but NOT pure: _parse_book_stats stamps the stats ages off time.time(),
    so two calls on one book return different `oi_age_s`/`last_trade_age_s`. Compare two results
    only with the clock pinned. get_fill_quote owns the fetch and its fail-closed error path."""
    state = md.get("state", "?")
    transact_time = md.get("transactTime")
    stats = _parse_book_stats(md, time.time())
    raw = md.get("bids", []) if is_short else md.get("offers", [])
    ask_levels: list[tuple[float, float]] = []
    for lvl in (raw or []):
        p = _amount_to_float(lvl.get("px")) if lvl.get("px") else None
        if p is None:
            continue
        ask_price = round(1.0 - p, 6) if is_short else p   # normalize short → ask space
        ask_levels.append((ask_price, float(lvl.get("qty") or 0)))
    if not ask_levels:
        return None, state, [], transact_time, stats
    ask = min(p for p, _ in ask_levels)
    return (ask if ask > 0 else None), state, ask_levels, transact_time, stats


def order_is_filled(resp: Optional[dict]) -> bool:
    """Return True only if a CreateOrderResponse shows a fully-filled order.

    Per the SDK types, CreateOrderResponse is {"id", "executions": [Execution]}
    and Execution.order is an Order carrying both `state` (an OrderState enum)
    and fill quantities (`cumQuantity` / `quantity`). A fully-filled FOK reports
    state == "ORDER_STATE_FILLED" OR cumQuantity >= quantity. A killed/rejected/
    expired FOK reports neither — a non-None response alone does NOT mean filled.

    CAVEAT: this only works if the create response actually carries the terminal
    execution state. If orders are placed without `synchronousExecution`, the
    response can return before the FOK resolves (empty executions / NEW state)
    even though it fills async — in which case no parsing here can detect it.
    See place_limit_fok.
    """
    if not isinstance(resp, dict):
        return False
    for ex in resp.get("executions", []) or []:
        order = ex.get("order", {}) if isinstance(ex, dict) else {}
        if order.get("state") == "ORDER_STATE_FILLED":
            return True
        cum, qty = order.get("cumQuantity"), order.get("quantity")
        if isinstance(cum, int) and isinstance(qty, int) and qty > 0 and cum >= qty:
            return True
    return False


def order_filled_qty(resp: Optional[dict]) -> float:
    """Return how many contracts actually filled (cumQuantity).

    Poly US coerces our FOK to IOC, so an order can PARTIAL-fill: the response
    carries the terminal execution with cumQuantity < quantity. Return the max
    cumQuantity seen across executions (it's cumulative/monotonic). 0 = no fill.
    Used to reconcile the hedge to the real fill instead of assuming all-or-none.

    ⚠️ The max() is LOAD-BEARING — `executions[0]` LIES. [VERIFIED 2026-07-15 on a real
    255/300 partial fill, scripts/poly_fok_probe.py] The FIRST execution's order carried
    `cumQuantity: 0, leavesQuantity: 300` while the order had genuinely filled 255; only a
    LATER execution carried 255. Reading executions[0] (or trusting the first order object
    you find) reports "no fill" on a leg that filled — under poly_first that is a naked,
    UNRECORDED Poly position: no hedge booked, invisible to the exposure caps and the
    strand alert. Never "simplify" this to the first/last execution.

    The FOK coercion itself is also VERIFIED on a real order (see place_limit_fok): we ask
    for FILL_OR_KILL, the exchange echoes IMMEDIATE_OR_CANCEL and partial-fills. The venue
    docs claim FOK is honored — they are wrong.
    """
    if not isinstance(resp, dict):
        return 0.0
    best = 0.0
    for ex in resp.get("executions", []) or []:
        order = ex.get("order", {}) if isinstance(ex, dict) else {}
        cum = order.get("cumQuantity")
        try:
            if cum is not None:
                best = max(best, float(cum))
        except (TypeError, ValueError):
            continue
    return best


def poly_avg_fill_cost(resp: Optional[dict]) -> Optional[float]:
    """ACTUAL fee-inclusive cost per share from a Poly create, or None if unreadable.

    Poly reports both halves, so the Θ·p·(1−p) model isn't needed here:
        avgPx                            — the real VWAP (IOC can sweep levels)
        commissionNotionalTotalCollected — the real commission, for the ORDER
    [VERIFIED on the real 2026-07-15 fill: 255 @ 0.0090, commission $0.1400 → 0.009549/share.]

    Reads the MAX-cumQuantity execution, not executions[0] — that one LIES (it reported
    cumQuantity 0 on an order that filled 255). Same rule as order_filled_qty.

    None = CANNOT READ; the caller falls back explicitly. Never 0.0 (that would book a free fill).
    """
    if not isinstance(resp, dict):
        return None
    best_cum, best = 0.0, None
    for ex in resp.get("executions", []) or []:
        order = ex.get("order", {}) if isinstance(ex, dict) else {}
        try:
            cum = float(order.get("cumQuantity") or 0)
        except (TypeError, ValueError):
            continue
        if cum <= 0 or cum < best_cum:
            continue
        px = _amount_to_float(order.get("avgPx"))
        comm = _amount_to_float(order.get("commissionNotionalTotalCollected"))
        if px is None or comm is None:
            continue
        best_cum, best = cum, px + comm / cum
    return best


class PolyUSClient:
    """Polymarket US CLOB client with dry-run support. Async-native."""

    def __init__(self) -> None:
        self._dry_run = config.DRY_RUN
        if not self._dry_run and (
            not config.POLYMARKET_US_KEY_ID or not config.POLYMARKET_US_SECRET_KEY
        ):
            raise ValueError(
                "Polymarket US requires POLYMARKET_US_KEY_ID and "
                "POLYMARKET_US_SECRET_KEY (set them in .env, or enable DRY_RUN)"
            )
        self._sdk = AsyncPolymarketUS(
            key_id=config.POLYMARKET_US_KEY_ID or None,
            secret_key=config.POLYMARKET_US_SECRET_KEY or None,
        )
        log.info("Polymarket US client initialized")
        if self._dry_run:
            log.info("DRY RUN mode enabled — no real orders will be placed")

    async def close(self) -> None:
        await self._sdk.close()

    async def get_usdc_balance(self, force_real: bool = False) -> float:
        """Return free-to-trade USD balance (buyingPower). -1.0 on error.

        In DRY_RUN this returns a 9999 sentinel so the executor's pre-trade check
        always "affords" simulated trades. Pass force_real=True to bypass that and
        fetch the actual balance (e.g. for FBAR/tax logging) — it's a read-only
        query, safe in any mode.
        """
        if self._dry_run and not force_real:
            return 9999.0
        try:
            resp = await self._sdk.account.balances()
            balances = resp.get("balances", []) if isinstance(resp, dict) else []
            for bal in balances:
                if bal.get("currency") == "USD":
                    return float(bal.get("buyingPower") or 0.0)
            return 0.0
        except Exception as exc:
            # Trim: a venue 5xx returns a multi-KB HTML error page (e.g. Cloudflare's ~50-line 504),
            # which floods stdout if logged raw. Collapse whitespace + cap to one short line.
            msg = " ".join(str(exc).split())[:200]
            log.error(f"PolyUSClient.get_usdc_balance failed: {msg}")
            return -1.0

    async def get_best_ask(self, market_slug: str) -> Optional[float]:
        """Return best ask for a market-side slug, or None if no ask / error."""
        try:
            resp = await self._sdk.markets.bbo(market_slug)
            md = resp.get("marketData", {}) if isinstance(resp, dict) else {}
            return _amount_to_float(md.get("bestAsk"))
        except Exception as exc:
            log.debug(f"PolyUSClient.get_best_ask({market_slug}): {exc}")
            return None

    async def get_fill_ask(self, token: str) -> Optional[float]:
        """Live fire-time ask for the TRADEABLE side of a token, from a FRESH REST bbo.

        Long token (bare slug) → bestAsk. Short token ("<slug>::short") → 1 − bestBid.
        Used to re-validate the price right before sending an order, independent of the
        WS feed's age — a stale feed quote vs a fresh opposing-venue quote manufactures
        phantom edges. None on error/missing so the caller can fail closed (skip)."""
        slug, is_short = parse_token(token)
        try:
            resp = await self._sdk.markets.bbo(slug)
        except Exception as exc:
            log.debug(f"PolyUSClient.get_fill_ask({token}): {exc}")
            return None
        md = resp.get("marketData", {}) if isinstance(resp, dict) else {}
        if is_short:
            bid = _amount_to_float(md.get("bestBid"))
            return round(1.0 - bid, 6) if (bid is not None and bid > 0) else None
        ask = _amount_to_float(md.get("bestAsk"))
        return ask if (ask is not None and ask > 0) else None

    async def _fetch_book(self, slug: str, *, fresh: bool):
        """Single choke-point for every order-book GET, so the cached-vs-live decision lives in
        ONE place (no caller can silently read stale). fresh=True appends a nonce query param so
        the read bypasses Polymarket's 30s Cloudflare /book cache (cf=MISS → origin) — REQUIRED
        for anything feeding a live decision, sizing, or the strand-unwind. fresh=False stays
        cacheable for bulk/discovery. Same SDK _request → response.json() either way; only the
        cache key differs (locked by the structural-equivalence test)."""
        if fresh:
            return await self._sdk.get(
                f"/v1/markets/{slug}/book",
                query={"_": str(int(time.time() * 1000))},   # nonce → CF cache-bust
            )
        return await self._sdk.markets.book(slug)

    async def get_fill_quote(
        self, token: str, *, fresh: bool = False
    ) -> tuple[Optional[float], str, list[tuple[float, float]], Optional[str], dict]:
        """Fire-time (ask, market_state, ask_levels, transact_time, stats) for the token's
        tradeable side, from the order BOOK (which carries `state`; bbo does not). One book fetch
        yields all five — no extra round-trip.

        ask_levels is the tradeable side normalized to ASK space — [(ask_price, qty), ...]
        (long=offers as-is; short=(1−bid_px, qty)) — so the caller can sum FILLABLE-AT-LIMIT
        depth (qty across every level at-or-better than the price it will pay), matching how
        the Kalshi leg is sized (_rest_fillable). Best-level-only depth would overstate
        fillable size in thin books — exactly where it matters.

        transact_time is the book's server-side mutation timestamp (marketData.transactTime,
        ISO-8601), or None when absent — a freshness signal callers log (see transact_age_s).

        fresh=True appends a nonce query param so the read bypasses the 30s Cloudflare cache
        (cf=MISS) and reaches origin; default reads stay cacheable for bulk/discovery callers.
        Both paths go through the same SDK _request → response.json(), so they return the same
        book shape — only the cache key differs (locked by a structural-equivalence test).

        market_state matters: after a goal Poly SUSPENDS the market, freezing a stale
        price that is NOT tradeable — firing into it would reject or fill post-unhalt at
        a bad price. The caller fires only when state == MARKET_STATE_OPEN. Returns
        (None, state, [], transact_time, stats) when there's no quote; (None, '?', [], None,
        empty-stats) on error → fails closed.

        stats = liquidity/activity from marketData.stats (OI / last-trade / shares / notional +
        age stamps) — LOGGING-ONLY, parsed from the SAME fetch (free); all-None when absent and
        never raises into the fire path (see _parse_book_stats). The first four values are the
        fire-path authority and are computed exactly as before — stats is purely additive."""
        slug, is_short = parse_token(token)
        try:
            book = await self._fetch_book(slug, fresh=fresh)
        except Exception as exc:
            log.debug(f"PolyUSClient.get_fill_quote({token}): {exc}")
            return None, "?", [], None, dict(_EMPTY_BOOK_STATS)
        md = book.get("marketData", {}) if isinstance(book, dict) else {}
        return quote_from_md(md, is_short)

    async def get_book(self, market_slug: str) -> Optional[dict]:
        """Return raw order book dict (bids/offers) for a market-side slug."""
        try:
            return await self._sdk.markets.book(market_slug)
        except Exception as exc:
            log.debug(f"PolyUSClient.get_book({market_slug}): {exc}")
            return None

    async def get_book_depth(self, token: str) -> Optional[float]:
        """Authoritative REST depth (shares) at the tradeable side's best level — for the
        would-fire SAMPLER to log alongside the freeze-prone WS depth, so phantom depth can
        be caught by ground truth instead of inference. long=offers@min-px, short=bids@max-px.
        None on error/empty. Deliberately NOT used in the fire path (that fix is deferred)."""
        slug, is_short = parse_token(token)
        try:
            book = await self._sdk.markets.book(slug)
        except Exception as exc:
            log.debug(f"PolyUSClient.get_book_depth({token}): {exc}")
            return None
        md = book.get("marketData", {}) if isinstance(book, dict) else {}
        levels = md.get("bids", []) if is_short else md.get("offers", [])
        pxs = [p for p in (_amount_to_float(l.get("px")) for l in (levels or []) if l.get("px"))
               if p is not None]
        if not pxs:
            return None
        best = max(pxs) if is_short else min(pxs)
        return sum(
            float(l.get("qty") or 0)
            for l in levels
            if l.get("px") and _amount_to_float(l.get("px")) == best
        )

    async def get_settlement(self, token: str) -> Optional[float]:
        """Return the market's LONG-side settlement price (1.0=long won, 0.0=long lost,
        intermediate=void/LFMP fair-value mark), or None if not settled / on error.

        Side-agnostic: settlementPrice is a property of the slug, so the short/long
        suffix is irrelevant here — the caller (bot.kalshi.settlement) applies the side.
        Read-only (a GET); safe in any mode, including DRY_RUN."""
        slug, _is_short = parse_token(token)
        try:
            resp = await self._sdk.markets.settlement(slug)
        except Exception as exc:
            log.debug(f"PolyUSClient.get_settlement({slug}): {exc}")
            return None
        if not isinstance(resp, dict):
            return None
        # The live API returns a plain numeric `settlement` (e.g. {"slug":..,"settlement":0}),
        # NOT the `settlementPrice: Amount` the SDK type claims. Accept either, and a bare
        # number. Use `is not None` — a definite long-loss settles at 0 (falsy but valid),
        # only a MISSING field means not-yet-settled.
        raw = resp.get("settlement")
        if raw is None:
            raw = resp.get("settlementPrice")
        if raw is None:
            return None
        if isinstance(raw, dict):
            return _amount_to_float(raw)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def place_limit_fok(
        self, token: str, price: float, size: float, label: str = ""
    ) -> Optional[dict]:
        """
        Place a FOK (Fill-or-Kill) limit BUY on the given market token.

        For a moneyline game ONE slug carries both teams: a bare slug buys the
        long side (team A); a "<slug>::short" token buys the short side (team B)
        via BUY_SHORT. `price` is that side's price (long ask, or short = 1 − bid).
        DRY_RUN only logs.

        ⚠️ MISNOMER — this does NOT place a fill-or-kill order. **Polymarket US does not
        honor FOK: it silently REWRITES tif FILL_OR_KILL → IMMEDIATE_OR_CANCEL.** The
        venue DOCS claim otherwise ("must fill entirely or cancel") — the docs are wrong;
        don't "restore" FOK on their say-so. [VERIFIED 2026-07-15 on a REAL order,
        scripts/poly_fok_probe.py: sent FOK for 300 into 255 of depth → echoed tif came
        back IOC and it PARTIAL-FILLED 255/300, the exact outcome FOK forbids.]

        So the leg is IOC and **CAN PARTIAL-FILL**. There is no "fills completely or is
        killed" guarantee to lean on (a previous version of this docstring claimed one —
        it was never true). The stranded-leg invariant is preserved NOT by the tif but by
        callers sizing the opposite leg off the ACTUAL fill this returns — see
        order_filled_qty and kalshi_arb._place_poly. Any new caller MUST reconcile to the
        real fill; assuming all-or-none books a phantom hedge and leaves a naked leg.
        """
        market_slug, is_short = parse_token(token)
        intent = "ORDER_INTENT_BUY_SHORT" if is_short else "ORDER_INTENT_BUY_LONG"
        tag = "[DRY RUN] " if self._dry_run else ""
        log.info(
            f"{tag}ORDER(US)  slug={market_slug}  side={'short' if is_short else 'long'}  "
            f"price={price:.4f}  size={size:.0f} shares  cost=${price * size:.2f}  {label}"
        )
        if self._dry_run:
            return {"status": "dry_run", "market_slug": market_slug,
                    "price": price, "size": size}
        try:
            return await self._sdk.orders.create({
                "marketSlug": market_slug,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{price:.4f}", "currency": "USD"},
                "quantity": int(round(size)),
                # IOC, not FOK. Poly does not honor FOK — it silently rewrites it to IOC
                # [VERIFIED 2026-07-15 on a real order: 255/300 partial]. Asking for a guarantee
                # we never receive is how a false safety claim survived in this docstring. Naming
                # what we actually get also makes us independent of Poly's roadmap: if they ever
                # SHIP real FOK, an order asking for FOK would silently become all-or-nothing —
                # changing fill rates and breaking the data regime with no code change.
                # And IOC is what we'd choose anyway: a partial fill is a smaller arb, which beats
                # the nothing that FOK returns (measured: 87 events where the book held a median 3
                # against a median-7 target).
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                # Block until the FOK is terminal so the response carries the real
                # executions/state. Without this, create() can return before the
                # order resolves and a real fill reads as a miss → stranded leg.
                "synchronousExecution": True,
            })
        except Exception as exc:
            # AN ERROR IS NOT AN OUTCOME — it used to `return None`, and that is how an order
            # whose fate we do not know became "filled nothing". `_place_poly` fed the None to
            # `order_filled_qty` → 0.0 → `_exec_poly_first` took its `P <= 0` branch, alerted
            # "MISSED (no fill)", and returned, under a comment reading "no exposure". A genuine
            # IOC kill is a 200 with cumQuantity 0; this path is ONLY timeout / 502 / reset —
            # exactly the case where the engine may already have filled. `synchronousExecution`
            # blocks the order ~61ms server-side, so a timeout lands squarely inside the fill
            # window. The Kalshi client states this rule for itself in _post ("a FOK kill is an
            # OUTCOME, NOT AN ERROR") and raises here; Poly, which fires FIRST in the deployed
            # config, made the opposite choice one layer lower and silently.
            #
            # Callers must distinguish "the venue said zero" from "we do not know". Raising is the
            # only way to say the second. Both exec helpers now catch it (see kalshi_arb), and
            # scripts/verify_execution.py is safer for it too — it used to print "❌ FAIL: buy did
            # not fill" on an error and skip its sell_back, having possibly just bought.
            log.error(f"PolyUSClient order failed for {market_slug}: {exc}")
            raise

    async def sell_back(
        self, token: str, size: float, label: str = ""
    ) -> tuple[Optional[float], float]:
        """
        Emergency sell of a stranded position at the best available price.

        Returns **(vwap_price, sold_qty)** — `(None, 0.0)` if nothing sold.

        Long leg → SELL_LONG at the best bid. Short leg ("<slug>::short") →
        SELL_SHORT at the short bid = 1 − best long ask (crosses the offers).
        Tries the best price, then retries 2¢ more aggressively for WHATEVER IS
        STILL UNSOLD. Preserves the stranded-leg unwind invariant for Poly US.

        ⚠️ This is NOT a fill-or-kill sell, despite the tif we send: Poly rewrites
        FOK → IOC [VERIFIED 2026-07-15 on a real order — see place_limit_fok], so a
        sell **CAN PARTIAL-FILL**. That is why this returns the sold QUANTITY and not
        just a price: a caller given only a price cannot tell a full sale from a
        partial one, and must strand only the UNSOLD remainder.

        History — the bug this shape exists to prevent (fixed 2026-07-15): the old
        version returned Optional[float] and its retry closed over the ORIGINAL `size`.
        A partial first attempt (60/100) therefore (a) read as a total failure, because
        order_is_filled requires a FULL fill, (b) re-sent quantity=100 on the retry while
        only 40 were still held — an OVERSELL, and (c) ended up reporting None, so the
        caller stranded 100 phantom shares and the proceeds of the 60 that really sold
        never reached P&L. Every one of those followed from assuming FOK semantics the
        venue does not provide.

        The reported price is the qty-weighted mean of the LIMITS we sold at, which is
        conservative for a sell (a real fill is at-or-better than our limit → we receive
        at-least this → the flatten cost we book is an upper bound, never flattering).
        """
        market_slug, is_short = parse_token(token)
        side = "short" if is_short else "long"
        intent = "ORDER_INTENT_SELL_SHORT" if is_short else "ORDER_INTENT_SELL_LONG"
        tag = "[DRY RUN] " if self._dry_run else ""
        log.warning(f"{tag}SELL-BACK(US)  slug={market_slug}  side={side}  size={size:.2f}  {label}")
        if self._dry_run:
            return 0.50, float(size)

        try:
            # fresh=True: the unwind price MUST come from the live book, not Polymarket's
            # 30s Cloudflare cache — a stale price would let the FOK miss the real book and
            # strand the leg (the exact failure this unwind prevents).
            book = await self._fetch_book(market_slug, fresh=True)
            # Real response nests the book under "marketData" (like bbo) — the
            # SDK's MarketBook type wrongly claims top-level "bids". Reading the
            # wrong path returned [] every time → "no bids" → unwind always
            # failed and stranded the leg. Read marketData.{bids,offers}.
            md = book.get("marketData", {}) if isinstance(book, dict) else {}
            if is_short:
                # Selling a short position crosses the LONG offers: short bid =
                # 1 − best (lowest) long ask.
                offers = md.get("offers", []) if isinstance(md, dict) else []
                ask_prices = [
                    _amount_to_float(lvl.get("px")) for lvl in offers
                    if _amount_to_float(lvl.get("px")) is not None
                ]
                if not ask_prices:
                    log.error(f"No offers available to sell-back (short) {market_slug}")
                    return None, 0.0
                best_price = 1.0 - min(ask_prices)
            else:
                bids = md.get("bids", []) if isinstance(md, dict) else []
                bid_prices = [
                    _amount_to_float(lvl.get("px")) for lvl in bids
                    if _amount_to_float(lvl.get("px")) is not None
                ]
                if not bid_prices:
                    log.error(f"No bids available to sell-back {market_slug}")
                    return None, 0.0
                best_price = max(bid_prices)
        except Exception as exc:
            log.error(f"PolyUSClient.sell_back book fetch failed for {market_slug}: {exc}")
            return None, 0.0

        async def _try_sell(price: float, qty: float) -> float:
            """Sell `qty` at `price`; return the qty ACTUALLY sold (0.0 on miss/error).

            Sizing per-attempt (not off the closure) is what stops the retry overselling.
            Reads the real fill via order_filled_qty — NOT order_is_filled, which demands a
            FULL fill and so reports a genuine partial sale as "nothing happened"."""
            try:
                resp = await self._sdk.orders.create({
                    "marketSlug": market_slug,
                    "intent": intent,
                    "type": "ORDER_TYPE_LIMIT",
                    "price": {"value": f"{price:.4f}", "currency": "USD"},
                    "quantity": int(round(qty)),
                    # IOC (Poly rewrites FOK→IOC anyway) — and IOC is right for an unwind:
                    # selling PART of a stranded leg beats selling none. The caller strands only
                    # the unsold remainder.
                    "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
                    "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                    "synchronousExecution": True,
                })
                # synchronousExecution → the response is authoritative for the real fill.
                sold = order_filled_qty(resp)
                # A response can report terminal FILLED without echoing cumQuantity (the
                # order_is_filled path this replaced relied on exactly that). FILLED means the
                # WHOLE requested qty filled by definition, so trust it rather than read 0.
                # Direction matters: UNDER-reporting a sale is the dangerous way to be wrong —
                # it strands shares we already sold and re-offers them on the retry. Over-
                # reporting can't happen here (state==FILLED is only ever a complete fill).
                if sold <= 0 and order_is_filled(resp):
                    sold = float(int(round(qty)))
                return sold
            except Exception as exc:
                log.debug(f"Sell-back attempt at {price:.4f} missed: {exc}")
                return 0.0

        # Best price first, then 2¢ through it — each attempt sized to what is STILL HELD.
        remaining, sold_total, proceeds = float(size), 0.0, 0.0
        for i, price in enumerate((best_price, max(0.01, best_price - 0.02))):
            if remaining < 1:
                break
            if i:
                log.warning(
                    f"Retrying sell-back(US) for the unsold {remaining:.0f} at discount: {price:.4f}"
                )
            filled = await _try_sell(price, remaining)
            sold_total += filled
            proceeds += price * filled
            remaining -= filled

        if sold_total <= 0:
            log.error(f"Sell-back(US) failed for {market_slug} ({side}) — leg remains stranded")
            return None, 0.0
        vwap = proceeds / sold_total
        if remaining >= 1:
            # Sold SOME. The caller must strand only `remaining`, and book the real proceeds.
            log.critical(
                f"Sell-back(US) PARTIAL for {market_slug} ({side}): sold {sold_total:.0f}/{size:.0f} "
                f"@ ~{vwap:.4f} — {remaining:.0f} still HELD"
            )
        else:
            log.info(f"Sell-back(US) succeeded for {market_slug} ({side}) at {vwap:.4f}")
        return vwap, sold_total
