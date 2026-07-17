"""Shared data types used across venues (Polymarket, Poly US, Kalshi)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MarketPair:
    """
    A binary head-to-head event: two mutually exclusive outcome tokens.
    Exactly one token always pays $1.
    """
    event_id: str
    event_title: str

    token_yes_a: str   # Token for side A (or YES of market A)
    token_no_a: str    # Token for ~A  (complement of A, or NO of market A)
    question_a: str

    token_yes_b: str   # Token for side B (or YES of market B)
    token_no_b: str    # Token for ~B
    question_b: str

    start_date: str | None = None  # ISO8601 event start time (for cross-platform matching)
    end_date: str | None = None    # ISO8601 event end/resolution time (the active-game-window END)
    # Poly taker fee coefficient Θ: fee = round(Θ·C·p·(1−p), 2), rounded to the CENT on the ORDER
    # total [VERIFIED 2026-07-15 against 9 real exchange commissions]. In `us` mode this holds the
    # market's own `feeCoefficient` (0.06 today — Poly RAISED it from 0.05 between 2026-06-17 and
    # 2026-07-15), read PER-MARKET by scanner._event_fee_rate. It is NOT the legacy global-Poly
    # "feeRate" the old comment named. 0.0 only on the dormant global path, which never sets it.
    #
    # Read this DIRECTLY — there is deliberately no on/off flag. A `fees_enabled: bool = False`
    # gate used to sit here and cross_arb read `taker_fee_rate if fees_enabled else 0.0`, so any
    # construction site that forgot the flag SILENTLY zeroed the fee (overstating every Poly edge
    # by up to 1.5¢/share → firing trades never above the 2% floor). It also contradicted
    # _coerce_fee_rate, which fails CLOSED ("NEVER 0") while the flag failed OPEN. This field is
    # already self-fail-safe; don't re-add a way to silence it.
    taker_fee_rate: float = 0.0
    settlement_type: str = ""      # Poly sportsMarketType (e.g. "drawable_outcome");
    # drives the settlement-equivalence gate. "" = unknown → fail-closed (not tradeable).
    kalshi_series: str = ""         # expected Kalshi series ticker for THIS Poly pair's sport
    # (e.g. "KXMLBGAME"). The cross-venue matcher requires the Kalshi market's series to equal this
    # — a SPORT-consistency gate so a Poly MLB "Minnesota" can't match a Kalshi WNBA "Minnesota"
    # (different games → phantom edge). "" = unknown sport → fail-closed (matches nothing).
    poly_tick: float | None = None  # the market's own `orderPriceMinTickSize`, read per-market by
    # scanner._event_poly_tick. LOGGING-ONLY (would_fire.csv) — no gate, limit, or size reads it.
    # None = unread, and specifically NOT a default: the tick is per-MARKET and varies WITHIN a
    # series (MLB 0.005, NBA/WNBA 0.01, WC both — VERIFIED 2026-07-16, venue-reference skill), so
    # any default would report a plausible-looking wrong tick.
    # It USED to decide the Poly buffer's fate (a real cushion on 0.005, floored away on 0.01) —
    # that split is what got the buffer dropped (S4, 2026-07-16 → COMPLETED.md). The Poly limit is
    # now the raw ask, flat, so nothing computes with this. Kept because it is the column that
    # caught the wrong per-series tick claim on its first row, and the read side still needs it.
    # The dormant global path never sets it.
