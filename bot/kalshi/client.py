"""
bot/kalshi_client.py
────────────────────
Kalshi REST + WebSocket auth client.

Auth: every request requires three headers derived from a fresh timestamp:
  KALSHI-ACCESS-KEY        — API key ID (from Kalshi dashboard)
  KALSHI-ACCESS-SIGNATURE  — base64(sign(ts_ms + METHOD + path))
  KALSHI-ACCESS-TIMESTAMP  — Unix milliseconds as string

Signing is RSA-PSS (SHA-256) for RSA keys — Kalshi's default scheme — or Ed25519
if the key is an Ed25519 key. _sign auto-detects from the loaded key type.
"""
from __future__ import annotations

import base64
import time
from typing import Any

import aiohttp

from bot.core import config
from bot.core.logger import get_logger

log = get_logger(__name__)

_PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
_DEMO_REST = "https://external-api.demo.kalshi.co/trade-api/v2"
_PROD_WS   = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
_DEMO_WS   = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

_WS_PATH   = "/trade-api/ws/v2"
_REST_PATH = "/trade-api/v2"

# Kalshi V2 signals a killed FOK with a 409 carrying this code (V1 returned a body with
# fill_count=0). It is an OUTCOME, not an error — see _post. Same string the RTT probe keys on.
_FOK_KILLED = "fill_or_kill_insufficient_resting_volume"

# Hard bound on an ORDER round-trip. aiohttp's DEFAULT is total=300s (5 MINUTES) — an unexamined
# default from before anyone had measured an RTT. The measured Kalshi order RTT is median 17ms /
# max 24ms [scripts/order_rtt_probe, prod 2026-07-14], so 300s is 12,500x the worst case.
# Why it matters: _execution_lock is ONE GLOBAL lock (runner.py) shared by kalshi_arb, poly_arb,
# ladder and macro, and poly_first fires POLY FIRST. So a hung Kalshi POST means we sit on a NAKED,
# unhedged Poly leg while EVERY trading path in the bot is frozen — for up to five minutes.
# 5s = ~200x the max observed order RTT: generous for a real slow path, bounded for a hung one.
# Scoped to _post (the order path / the lock-holder); bulk discovery GETs keep the session default.
_ORDER_TIMEOUT = aiohttp.ClientTimeout(total=5, sock_connect=2)

# /portfolio/positions is cursor-paginated. 200 is the venue's max page size; the page stop mirrors
# the Poly side of the reconciler. Hitting the stop with a cursor still set RAISES — a truncated
# read of what we hold must not be reported as a complete one (see get_positions).
_POSITIONS_PAGE_LIMIT = 200
_POSITIONS_MAX_PAGES = 20



# (v1_side, v1_action) → V2 book side. V2 is YES-ONLY: `bid` buys YES, `ask` sells YES; there is
# no `action` field and no "buy NO". Buying NO is expressed as selling YES at the COMPLEMENT.
# The NO side flips BOTH the book side AND the price — flipping only one buys the wrong side.
# CONFIRMED BY THE EXCHANGE 2026-07-14: we sent {side:"ask", price:"0.3000"} holding zero YES and
# Kalshi's fill record came back {"side":"no","no_price_dollars":"0.7000"} with position_fp=-1.00.
_V2_BOOK_SIDE = {
    ("yes", "buy"): "bid",    # buy YES  @ p        → bid @ p
    ("yes", "sell"): "ask",   # sell YES @ p        → ask @ p
    ("no", "buy"): "ask",     # buy NO   @ n        → ask @ (1-n)
    ("no", "sell"): "bid",    # sell NO  @ n        → bid @ (1-n)
}


def _v2_order_params(side: str, action: str, price_dollars: str | float) -> tuple[str, float]:
    """Map V1 (side, action, side-space price) → V2 (book_side, YES price).

    V2 quotes everything from the YES side, so a NO order must be complemented: `price` is ALWAYS
    the YES price. This is the 1-x footgun that once stranded every NO leg under V1 — pinned by
    tests/test_kalshi_v2_orders.py against the exchange's own fill record.

    TICK SAFETY: callers tick-floor in the SIDE'S OWN space (kalshi_tick_floor) BEFORE calling, and
    1 − (multiple of 0.01) is still a multiple of 0.01, so complementing AFTER that floor is exact
    and preserves the economic bound (pay ≤ limit). Do NOT reorder: complementing before the floor
    inverts the rounding direction (flooring 1−n makes us pay MORE for NO, not less).
    """
    key = (str(side).lower(), str(action).lower())
    if key not in _V2_BOOK_SIDE:
        raise ValueError(f"unknown Kalshi side/action combination: {side!r}/{action!r}")
    p = float(price_dollars)
    yes_price = p if key[0] == "yes" else round(1.0 - p, 6)
    return _V2_BOOK_SIDE[key], yes_price


def _order_counts(resp) -> tuple[float | None, float | None]:
    """(fill, remaining) from EITHER order response shape, else (None, None).

    V2 CREATE (POST /portfolio/events/orders) is FLAT: {fill_count, remaining_count, ...} with NO
    `order` envelope and NO `status`. GET /portfolio/orders/{id} is NESTED: {order:{fill_count_fp,
    remaining_count_fp, status}}. The V1 parsers only read the nested shape, so against a V2 create
    they silently returned 0 — which reads as "the Kalshi leg missed" on a leg that actually FILLED,
    flattening Poly while holding Kalshi (a naked position the bot doesn't know it has).
    """
    if not isinstance(resp, dict):
        return (None, None)
    if "fill_count" in resp or "remaining_count" in resp:      # V2 create (flat)
        f, r = resp.get("fill_count"), resp.get("remaining_count")
    else:                                                       # GET / DRY / legacy (nested)
        o = resp.get("order")
        if not isinstance(o, dict):
            return (None, None)
        if "fill_count_fp" not in o and "remaining_count_fp" not in o:
            return (None, None)
        f, r = o.get("fill_count_fp"), o.get("remaining_count_fp")
    try:
        return (float(f or 0), float(r or 0))
    except (TypeError, ValueError):
        return (None, None)


def kalshi_order_filled(resp) -> bool:
    """True if a Kalshi FOK FULLY filled. Handles both the V2 flat and nested shapes.

    Fill counts are authoritative and checked STRICTLY — a fully-filled FOK has fill > 0 and
    remaining == 0, so a partial never reads as a full fill even if a status says 'executed'.
    V2 create responses carry no `status` at all; the status fallback exists only for the DRY
    stub and legacy/GET fixtures.
    """
    fill, remaining = _order_counts(resp)
    if fill is not None:
        return fill > 0 and remaining == 0
    o = resp.get("order") if isinstance(resp, dict) else None
    return isinstance(o, dict) and o.get("status") in ("executed", "filled")


def kalshi_avg_fill_cost(resp) -> float | None:
    """ACTUAL fee-inclusive cost per contract from a V2 create, or None if unreadable.

    The exchange reports both halves, so no fee model prices the trade:
        average_fill_price  — the real VWAP (an IOC can sweep several levels), PER CONTRACT
        average_fee_paid    — the real fee. **Per contract or per order? WE DO NOT KNOW.**
    [The price VERIFIED on a real prod fill: 1 YES @ 0.4270 → 0.4270/contract.]

    ⚠️ `average_fee_paid`'s BASIS IS UNMEASURED, and it cannot be measured read-only — the field
    exists on the create response and on NO other endpoint (checked 2026-07-16: /portfolio/fills
    reports `fee_cost`, /portfolio/orders reports `taker_fees_dollars`, neither is this field).
    Our only real fixture is `fill_count=1`, where **per-contract and per-order are arithmetically
    identical**, so the measurement that "verified" this could never have discriminated. The
    predecessor of this function asserted "for the ORDER (pairs with fill_count)" and divided; its
    test pinned that with a FABRICATED fixture (4 @ 0.50 → fee 0.0400, which matches neither the
    real order total 0.0700 nor the real per-contract 0.0175). Kalshi's docs say "per contract",
    and its sibling `average_fill_price` certainly is — but this repo has been burned once already
    by reading a venue doc instead of a fill, and that is exactly how the cent-vs-centicent bug got
    in.

    So we DON'T guess: the response identifies itself. The fee FORMULA is verified to the
    centicent against 7 real fills (tests/test_kalshi_fee_model.py) and the two candidates differ
    by a factor of `fill_count`, so we compute both and take whichever the venue's own number
    matches. At fill_count=1 they coincide and the answer is the same either way. Guessing is not
    an option worth taking: at the 8-share ramp, reading a per-contract fee as an order total
    UNDERSTATES cost by ~1.5c/share — 77% of the 2% minimum edge, flattering `realized_settled`,
    the designated sizing authority — while the mirror mistake OVERSTATES by ~12c/share, which
    would make every hedge book as catastrophically unprofitable and trip the cumulative loss cap.

    Matching NEITHER candidate returns None (the caller falls back) and logs loudly: it means the
    fee schedule moved or the field changed, and either way we must not price a real position off
    a number we no longer understand.

    Why it matters: _record_hedge books this into trades.log's `cost`, and settlement_scorer reads
    that same `cost` for realized_settled — the sizing authority.

    None = CANNOT READ; the caller falls back explicitly. NEVER return 0.0 — that books a FREE
    fill, the most flattering possible lie about a leg we actually paid for. A price with no fee is
    also None: booking it fee-free under-costs the leg (the failure _coerce_fee_rate exists to
    prevent).
    """
    if not isinstance(resp, dict):
        return None
    try:
        fill = float(resp.get("fill_count") or 0)
        px = resp.get("average_fill_price")
        fee = resp.get("average_fee_paid")
        if fill <= 0 or px is None or fee is None:
            return None
        px, fee = float(px), float(fee)
    except (TypeError, ValueError):
        return None
    got = _kalshi_px_and_fee(px, fee, fill, "kalshi_avg_fill_cost")
    if got is None:
        return None
    px, fee_pc = got
    return px + fee_pc


def _kalshi_px_and_fee(px: float, fee: float, fill: float,
                       who: str) -> tuple[float, float] | None:
    """(avg_fill_price, fee PER CONTRACT) — resolving `average_fee_paid`'s unknown basis against
    the verified fee formula. See kalshi_avg_fill_cost for the full why. None = matches neither
    candidate, i.e. we no longer understand the number and must not price a position off it.

    Shared by the BUY reader (cost = px + fee) and the SELL reader (proceeds = px − fee), because
    the basis question is identical for both and answering it twice would let them drift.
    """
    # Local import: cross_arb does not import this module, so there is no cycle, but keep it
    # narrow — the client should not depend on the arb math at module scope.
    from bot.kalshi.cross_arb import _kalshi_taker_fee
    per_contract = _kalshi_taker_fee(px, int(fill) or 1)
    order_total = per_contract * fill
    tol = 5e-4                                  # a few centicents of slack around the ceiling
    if abs(fee - per_contract) <= tol:
        return px, fee                          # the field is PER CONTRACT
    if abs(fee - order_total) <= tol:
        return px, fee / fill                   # the field is the ORDER TOTAL
    log.error(
        f"{who}: average_fee_paid={fee:.6f} matches NEITHER the per-contract fee "
        f"({per_contract:.6f}) nor the order total ({order_total:.6f}) for {fill:.0f} @ {px:.4f} — "
        f"the fee schedule or the field changed. Refusing to price the leg; caller falls back."
    )
    return None


def kalshi_avg_sell_proceeds(resp) -> float | None:
    """NET proceeds per contract from a V2 SELL — the mirror of kalshi_avg_fill_cost.

    A buy PAYS the fee (cost = px + fee); a sell NETS it (proceeds = px − fee). Reading a sell with
    the buy helper would report proceeds ~2 fees too high and make every unwind look cheaper than
    it was — on a number the loss cap reads.

    Exists because the unwind used to book its exit at the LIMIT it sent (`sell_price`) and discard
    `sell_resp` entirely, so an IOC that swept several levels booked as if it got the top of the
    book. None = unreadable → the caller falls back to the limit and says so.
    """
    if not isinstance(resp, dict):
        return None
    try:
        fill = float(resp.get("fill_count") or 0)
        px = resp.get("average_fill_price")
        fee = resp.get("average_fee_paid")
        if fill <= 0 or px is None or fee is None:
            return None
        px, fee = float(px), float(fee)
    except (TypeError, ValueError):
        return None
    got = _kalshi_px_and_fee(px, fee, fill, "kalshi_avg_sell_proceeds")
    if got is None:
        return None
    px, fee_pc = got
    return px - fee_pc


def kalshi_filled_qty(resp) -> float:
    """Contracts actually filled on a Kalshi order, else 0. Handles both response shapes.

    With IOC a thin book partial-fills (fill < count, remainder canceled). Read the REAL fill so
    the paired Poly leg is sized to it instead of assuming all-or-nothing (a stated safety
    invariant: size the second leg from the ACTUAL fill of the first).
    """
    fill, _ = _order_counts(resp)
    return fill if fill is not None else 0.0


class KalshiClient:
    def __init__(self) -> None:
        is_prod = config.KALSHI_ENV == "prod"
        self._api_key  = config.KALSHI_API_KEY
        self._base_url = _PROD_REST if is_prod else _DEMO_REST
        self.ws_url    = _PROD_WS   if is_prod else _DEMO_WS

        self._private_key = None
        if config.KALSHI_PRIVATE_KEY_PATH:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
                self._private_key = load_pem_private_key(f.read(), password=None)

        # One pooled session reused across requests — avoids a fresh TCP+TLS
        # handshake (~2-3 RTT) on every order. Created lazily in the event loop.
        self._session: aiohttp.ClientSession | None = None

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, ts_ms: int, method: str, path: str) -> str:
        """Sign `ts_ms + METHOD + path` with the configured private key."""
        if self._private_key is None:
            raise RuntimeError("Kalshi private key not configured (KALSHI_PRIVATE_KEY_PATH)")
        msg = f"{ts_ms}{method}{path}".encode()

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        if isinstance(self._private_key, Ed25519PrivateKey):
            sig = self._private_key.sign(msg)
        else:
            sig = self._private_key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str) -> dict[str, str]:
        """Return auth headers with a fresh timestamp + signature."""
        ts_ms = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY":       self._api_key,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts_ms, method, path),
            "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
            "Content-Type":            "application/json",
        }

    def ws_headers(self) -> dict[str, str]:
        """Auth headers for WebSocket connect (signs the WS path)."""
        return self._headers("GET", _WS_PATH)

    # ── REST helpers ──────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create + reuse one pooled session (keep-alive connections)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the pooled session (call on shutdown)."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self._base_url}{path}"
        session = await self._get_session()
        async with session.get(
            url,
            headers=self._headers("GET", f"{_REST_PATH}{path}"),
            params=params or {},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post(self, path: str, body: dict) -> Any:
        url = f"{self._base_url}{path}"
        session = await self._get_session()
        async with session.post(
            url,
            headers=self._headers("POST", f"{_REST_PATH}{path}"),
            json=body,
            timeout=_ORDER_TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                # Surface Kalshi's rejection reason — raise_for_status() alone
                # discards the body, which hid order rejections (e.g. invalid
                # price tick) and stranded the paired Poly leg silently.
                detail = await resp.text()
                # A FOK kill is an OUTCOME, NOT AN ERROR: the order reached the matching engine
                # and didn't fill. Return it as a ZERO-FILL response so callers reconcile it
                # through their normal kfill==0 path.
                #
                # ⚠️ This used to `raise`, and that was a naked-leg bug on the DEFAULT exec order.
                # V1 signalled a kill with a BODY (fill_count=0); V2 signals it with a 409. The V2
                # migration propagated that to scripts/order_rtt_probe.py but NOT here, and
                # _exec_poly_first has no try/except:
                #     P = await self._place_poly(...)        # the Poly leg FILLS
                #     kalshi_resp = await create_order(...)  # raised 409 on a kill
                #     await self._unwind_poly_excess(...)    # ← NEVER RAN
                # so a killed Kalshi leg left a naked, UNRECORDED Poly position: no hedge, no
                # strand, no global pause, no alert — on poly_first (the default) and on the MODAL
                # outcome (64.0% kalshi_moved). The old comment here reasoned "Kalshi-first means
                # it strands nothing (caller skips)" — true for kalshi_first, where the raise lands
                # BEFORE any Poly order; false for the default, where it lands AFTER Poly filled.
                if _FOK_KILLED in detail:
                    log.info(f"Kalshi POST {path} → FOK killed (thin book): {detail}")
                    return {"fill_count": 0, "remaining_count": float(body.get("count") or 0),
                            "status": "canceled", "_fok_killed": True}
                # EVERY other 4xx/5xx is a REAL error (bad tick, insufficient funds, auth) and
                # stays loud — raise_for_status() alone discarded the body, which once hid order
                # rejections and stranded the paired Poly leg silently.
                log.error(f"Kalshi POST {path} → {resp.status}: {detail}")
                raise RuntimeError(f"Kalshi {resp.status} on {path}: {detail}")
            return await resp.json()

    # ── Public REST methods ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Return available balance in dollars. API returns cents, so divide by 100."""
        data = await self._get("/portfolio/balance")
        return float(data["balance"]) / 100.0

    async def get_market(self, ticker: str) -> dict:
        """Return market details including yes_bid, no_bid, volume."""
        return await self._get(f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Return bids/asks for a market."""
        return await self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def get_events(self, series_ticker: str) -> list[dict]:
        """Return open events for a series, with nested markets."""
        data = await self._get(
            "/events",
            params={
                "series_ticker":       series_ticker,
                "status":              "open",
                "with_nested_markets": "true",
            },
        )
        return data.get("events", [])

    async def fetch_markets_raw(self, series_tickers: list[str]) -> list[dict]:
        """Return raw Kalshi market dicts (incl. floor_strike/cap_strike) across series.

        Reuses get_events but returns the unparsed market dicts so callers can
        read strike fields for scope-boundary checks (e.g. validate_macro_pairs).
        """
        out: list[dict] = []
        for series in series_tickers:
            try:
                events = await self.get_events(series)
            except Exception as e:
                log.error(f"fetch_markets_raw: error fetching {series}: {e}")
                continue
            for ev in events:
                out.extend(ev.get("markets", []))
        return out

    async def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_dollars: str,
        time_in_force: str = "fill_or_kill",
    ) -> dict:
        """
        Place an order on Kalshi via the **V2** endpoint (`POST /portfolio/events/orders`).

        The V1 endpoint (`/portfolio/orders`) is DEAD — 410 Gone `deprecated_v1_order_endpoint`
        (found 2026-06-26 by order_rtt_probe; ALL live order placement was broken and invisible
        because DRY short-circuits before the network). Migrated 2026-07-14.

        The CALLER-FACING signature is deliberately UNCHANGED from V1 — side/action with the price
        in THAT side's own space — so every call site (fire, unwind, ladder) keeps its existing
        limit and tick logic. The V2 translation happens here, at the last moment:

            V1 (side, action, price)      →  V2 (side, price)      [price is ALWAYS the YES price]
            yes / buy   @ p               →  bid @ p
            yes / sell  @ p               →  ask @ p
            no  / buy   @ n               →  ask @ (1-n)     ← the 1-x complement
            no  / sell  @ n               →  bid @ (1-n)     ← the 1-x complement

        V2 is YES-ONLY: `bid` buys YES, `ask` sells YES; there is no `action` and no "buy NO".
        Buying NO is selling YES at the complement — CONFIRMED by the exchange's own fill record
        (we sent {side:"ask", price:"0.3000"} holding zero YES; Kalshi booked side:"no" at
        no_price 0.7000, position_fp=-1.00). See _v2_order_params + tests/test_kalshi_v2_orders.py.

        ⚠️ The complement is the footgun that once "stranded/blocked every NO leg" under V1. It is
        exact ONLY because callers tick-floor in the side's own space first (see _v2_order_params).

        Args:
            ticker:        Market ticker, e.g. "KXNBAGAME-LAKCEL-JUN14"
            side:          "yes" or "no"  (caller-space; translated here)
            action:        "buy" or "sell" (caller-space; translated here)
            count:         Number of contracts (int; serialised as the V2 fixed-point STRING)
            price_dollars: Price of THIS side as a 4-decimal string (yes price for yes, no price
                           for no) — already tick-floored by the caller.
            time_in_force: "fill_or_kill" (default) | "immediate_or_cancel" | "good_till_canceled"

        Returns the raw V2 response — FLAT: {order_id, fill_count, remaining_count,
        average_fill_price, average_fee_paid, ts_ms}. NOTE there is **no `status`** and no `order`
        envelope; use kalshi_filled_qty / kalshi_order_filled, never resp["order"].
        """
        if config.DRY_RUN:
            log.info(
                f"[DRY RUN] Kalshi order: {action} {count}× {ticker} {side} "
                f"@ {side}_price={price_dollars} tif={time_in_force}"
            )
            return {"order": {"status": "dry_run"}}

        v2_side, yes_price = _v2_order_params(side, action, price_dollars)
        body = {
            "ticker":                     ticker,
            "side":                       v2_side,
            "price":                      f"{yes_price:.4f}",   # ALWAYS the YES price
            "count":                      f"{float(count):.2f}",  # V2 wants a fixed-point STRING
            "time_in_force":              time_in_force,
            # REQUIRED in V2. taker_at_cross cancels OUR taker order if it would cross our own
            # resting order — fail-closed. We are taker-only, so this should never trigger.
            "self_trade_prevention_type": "taker_at_cross",
        }
        return await self._post("/portfolio/events/orders", body)

    async def get_positions(self) -> list[dict]:
        """Every open MARKET position, following the cursor to the end.

        ⚠️ IT RETURNED `[]` UNCONDITIONALLY, FOREVER. The body was
        `data.get("positions", [])`, but the response is
        `{cursor, event_positions, market_positions}` — there is no top-level `positions` key
        [VERIFIED 2026-07-16 against the live endpoint]. So the reconciler's Kalshi half reported
        "confirmed flat" on every poll regardless of what we actually held, which is the exact
        fail-open its docstring forbids ("never [] on failure — an empty list means 'confirmed
        flat' and would mask exactly what we're hunting"). Every guard downstream — the isinstance
        check, `_first_qty` returning None, the never-[]-on-failure rule — sat behind this one
        line and never got a chance to run. It had zero callers until the reconciler was built,
        and its tests mock at this boundary, so the parse had never once executed.

        RAISES rather than returning `[]` on any shape it does not recognise, and that asymmetry
        is the whole design: `reconcile._fetch_kalshi_positions` maps an exception to CANNOT-VERIFY
        (alerts, does not pause) and an empty list to "Kalshi confirmed flat" (all clear). A shape
        we cannot read is not evidence of no position, so it must never take the second path. Same
        reason a truncated page raises instead of returning what it managed to read.

        `market_positions` is the right array, not `event_positions`: its items carry `ticker` and
        `position_fp`, which is exactly what the caller parses. Sign is the caller's problem and it
        handles it — Kalshi books a NO position as NEGATIVE `position_fp` (short YES == long NO)
        and `_first_qty` takes the absolute value, which matters because 209 of our 267 would-fires
        are the NO side.

        Paginates because the endpoint is cursor-based and page 1 alone would under-read — the
        same trap the Poly side of the reconciler paginates to avoid. `cursor` is `''` when there
        are no more pages [VERIFIED 2026-07-16].
        """
        out: list[dict] = []
        cursor, pages = "", 0
        while True:
            params: dict = {"limit": _POSITIONS_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/portfolio/positions", params=params)
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"Kalshi positions: expected an object, got {type(data).__name__} — "
                    f"refusing to report flat")
            markets = data.get("market_positions")
            if not isinstance(markets, list):
                raise RuntimeError(
                    f"Kalshi positions: `market_positions` missing or not a list "
                    f"(keys={sorted(data)!r}) — refusing to report flat")
            out.extend(m for m in markets if isinstance(m, dict))
            cursor, pages = data.get("cursor") or "", pages + 1
            if not cursor:
                return out
            if pages >= _POSITIONS_MAX_PAGES:
                raise RuntimeError(
                    f"Kalshi positions: cursor still set after {pages} pages — refusing to "
                    f"report a truncated list as complete")
