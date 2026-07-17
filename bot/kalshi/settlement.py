"""
bot/kalshi/settlement.py
────────────────────────
Cross-venue settlement classifier: given how Polymarket US and Kalshi each settled
a matched market, decide whether the hedge settled as designed and (optionally) what
it realized.

This is the measurement foundation the profit posture needs: it turns "did the edge
survive settlement?" from an assumption into an observation. It classifies into the
three outcomes the settlement-equivalence allowlist (matcher.py:42-55) is built around:

  • CLEAN      — both venues report DEFINITE, COMPLEMENTARY results: exactly one leg
                 pays $1, total payout per share-pair = $1 → the arb settled as designed.
  • VOID       — either venue is non-definite (Poly LFMP fair-value mark / Kalshi
                 cancellation). The two marks need not agree → bounded loss. P&L is NOT
                 computed here — it depends on two fair-value marks the backtest models
                 (SETTLEMENT_W_VOID); returning 0 would be a silent-loss bug.
  • DIVERGENCE — both definite but NON-complementary (venues declare opposite winners):
                 total payout ∈ {0, 2} → the hedge breaks (~full-stake loss, or a
                 windfall the backtest prices pessimistically). The Super Bowl case.

PENDING is returned when a market is not yet settled on BOTH venues — a skip, never a
result (so the caller doesn't score an unresolved game).

Pure `classify`/`realized_pnl` carry the logic (fully unit-testable, no I/O); the thin
`fetch_settlement` wrapper does the two reads via the existing clients. It NEVER writes
any P&L file — capture/recording is the caller's job (and capture-forward must never
touch execution_pnl.csv; see scripts/settlement_capture.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from bot.core.logger import get_logger
from bot.poly_us.sides import parse_token

log = get_logger(__name__)

# A settled definite price sits at 0 or 1; anything in between is an LFMP/void mark.
# Tolerance absorbs float noise and trivial rounding, NOT a real fair-value void
# (those land well inside, e.g. 0.43) — keep it tight so a void can't read as definite.
_DEFINITE_TOL = 1e-3


class Outcome(str, Enum):
    CLEAN = "clean"
    VOID = "void"
    DIVERGENCE = "divergence"
    PENDING = "pending"


@dataclass(frozen=True)
class SettlementResult:
    outcome: Outcome
    poly_payout: Optional[float]      # per-share settled value of the HELD Poly token (short-adjusted)
    kalshi_payout: Optional[float]    # per-contract settled value of the HELD Kalshi side
    poly_settlement_price: Optional[float]   # raw long-side settlement price (side-agnostic)
    kalshi_result: str
    detail: str = ""


def _poly_side_payout(
    settlement_price: Optional[float], is_short: bool
) -> tuple[Optional[float], bool]:
    """(held-token payout, is_definite). settlement_price is the LONG side's settled
    value; a short token's payout is its complement. Definite ⇔ the long price is at
    0 or 1 within tolerance. None price → (None, False)."""
    if settlement_price is None:
        return None, False
    definite = (
        abs(settlement_price - 1.0) <= _DEFINITE_TOL
        or abs(settlement_price) <= _DEFINITE_TOL
    )
    payout = (1.0 - settlement_price) if is_short else settlement_price
    return payout, definite


def _kalshi_side_payout(result: str, kalshi_side: str) -> tuple[Optional[float], bool]:
    """(held-side payout, is_definite). A definite Kalshi result is 'yes' or 'no';
    the held side pays $1 iff it matches the winning side. Anything else (''/void/
    cancelled) → (None, False)."""
    r = (result or "").strip().lower()
    side = (kalshi_side or "").strip().lower()
    if r not in ("yes", "no"):
        return None, False
    return (1.0 if r == side else 0.0), True


def _parse_dollar(v) -> Optional[float]:
    """Parse a Kalshi *_dollars string/number to float; None on missing/garbage."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def classify(
    poly_settlement_price: Optional[float],
    is_short: bool,
    kalshi_result: str,
    kalshi_side: str,
    poly_settled: bool = True,
    kalshi_settled: bool = True,
    *,
    kalshi_value: Optional[float] = None,
) -> SettlementResult:
    """Classify a hedge's settlement. Assumes the two legs were constructed
    complementary at detection (find_kalshi_arbs) — settlement's total payout reveals
    whether that held (CLEAN) or the venues diverged (DIVERGENCE).

    kalshi_value = the Kalshi YES-side settled value in dollars (`settlement_value_dollars`):
    0/1 for a definite result, an intermediate FAIR MARK for a void (`result='scalar'`).
    Used only to fill the VOID Kalshi payout — definite cases still resolve from `result`."""
    if not poly_settled or not kalshi_settled or poly_settlement_price is None:
        return SettlementResult(
            Outcome.PENDING, None, None, poly_settlement_price, kalshi_result,
            "not settled on both venues",
        )
    p_pay, p_def = _poly_side_payout(poly_settlement_price, is_short)
    k_pay, k_def = _kalshi_side_payout(kalshi_result, kalshi_side)
    if not p_def or not k_def:
        # VOID: a non-definite leg is fair-value-marked, not $1/$0. Poly's LFMP mark is
        # already in p_pay. Fill the Kalshi mark from settlement_value_dollars (the YES-side
        # fair value) — VERIFIED on real result='scalar' markets (KXMLBGAME-26JUN211420TORCHC
        # at 0.51/0.49, KXMLBGAME-26JUN181915SFATL-SF at 0.43; 2026-06-21/18). Held side =
        # mark (yes) or 1−mark (no). A missing Kalshi mark leaves k_pay None →
        # realized_pnl_live floors the loss (never zero).
        if not k_def and kalshi_value is not None:
            side = (kalshi_side or "").strip().lower()
            k_pay = kalshi_value if side == "yes" else 1.0 - kalshi_value
        return SettlementResult(
            Outcome.VOID, p_pay, k_pay, poly_settlement_price, kalshi_result,
            f"poly_definite={p_def} kalshi_definite={k_def} kalshi_mark={kalshi_value}",
        )
    total = p_pay + k_pay
    if abs(total - 1.0) <= _DEFINITE_TOL:
        return SettlementResult(
            Outcome.CLEAN, p_pay, k_pay, poly_settlement_price, kalshi_result, "",
        )
    return SettlementResult(
        Outcome.DIVERGENCE, p_pay, k_pay, poly_settlement_price, kalshi_result,
        f"poly_payout={p_pay} kalshi_payout={k_pay} total={total}",
    )


def realized_pnl(
    res: SettlementResult, shares: float, poly_eff: float, kalshi_eff: float
) -> Optional[float]:
    """Realized P&L for a hypothetical/actual hedge of `shares` at the given effective
    per-share costs. Defined only for CLEAN/DIVERGENCE (both legs definite); VOID and
    PENDING return None — void is modeled downstream via SETTLEMENT_W_VOID, never
    silently zeroed."""
    if res.outcome in (Outcome.CLEAN, Outcome.DIVERGENCE):
        assert res.poly_payout is not None and res.kalshi_payout is not None
        return shares * (res.poly_payout + res.kalshi_payout) - shares * (poly_eff + kalshi_eff)
    return None


def realized_pnl_live(
    res: SettlementResult, shares: float, poly_eff: float, kalshi_eff: float, w_void: float
) -> Optional[float]:
    """Realized signed P&L for the LIVE loss-cap writer. Unlike `realized_pnl` (which returns
    None for VOID, modeling it downstream via a weight), this resolves VOID to an ACTUAL number
    because the cumulative-loss cap needs a figure — an unscored void is invisible to the kill.

    - CLEAN / DIVERGENCE: payout − fee-inclusive entry cost (both legs definite). Negative on a
      both-legs-lose divergence (the max-loss mode the cap most needs).
    - VOID with BOTH legs marked (Poly LFMP in poly_payout + Kalshi fair price in kalshi_payout):
      the actual marked P&L.
    - VOID with EITHER mark missing/unparseable: FAIL CLOSED to a conservative loss floor
      −shares·w_void (SETTLEMENT_W_VOID) — NEVER zero. A void whose exact P&L is unknown must
      still book a loss against the cap; zero is the invisible-void bug the writer exists to fix.
    - PENDING: None (caller skips — retry next tick).

    `poly_eff + kalshi_eff` must be the FEE-INCLUSIVE per-share entry cost (caller derives it
    from trades.log's fee-inclusive `cost`, never the fee-light tracker basis)."""
    cost = shares * (poly_eff + kalshi_eff)
    if res.outcome in (Outcome.CLEAN, Outcome.DIVERGENCE):
        assert res.poly_payout is not None and res.kalshi_payout is not None
        return shares * (res.poly_payout + res.kalshi_payout) - cost
    if res.outcome == Outcome.VOID:
        if res.poly_payout is not None and res.kalshi_payout is not None:
            return shares * (res.poly_payout + res.kalshi_payout) - cost
        return -(shares * w_void)          # mark missing → conservative floor, never zero (4b)
    return None


def _kalshi_is_settled(market: dict) -> bool:
    """A Kalshi market is resolved once status is settled/finalized or a definite
    result is present. 'closed'/'active' (trading halted, not yet resolved) → not settled."""
    status = str(market.get("status", "")).strip().lower()
    result = str(market.get("result", "")).strip().lower()
    return status in ("settled", "finalized") or result in ("yes", "no")


async def fetch_settlement(
    poly_client,
    kalshi_client,
    poly_token: str,
    kalshi_ticker: str,
    kalshi_side: str,
) -> SettlementResult:
    """Read both venues' settlement and classify. Read-only; fails closed to PENDING on
    any fetch error (an unreadable settlement is 'not yet known', never a fabricated
    result). Poly settlement price comes from PolyUSClient.get_settlement; Kalshi from
    get_market (response nests the market under 'market')."""
    slug, is_short = parse_token(poly_token)

    poly_price: Optional[float] = None
    poly_settled = False
    try:
        poly_price = await poly_client.get_settlement(poly_token)
        poly_settled = poly_price is not None
    except Exception as exc:  # noqa: BLE001 — read must never raise into the caller
        log.debug(f"fetch_settlement poly {slug}: {exc!r}")

    kalshi_result = ""
    kalshi_value: Optional[float] = None
    kalshi_settled = False
    try:
        data = await kalshi_client.get_market(kalshi_ticker)
        market = data.get("market", data) if isinstance(data, dict) else {}
        kalshi_result = str(market.get("result", "") or "")
        kalshi_value = _parse_dollar(market.get("settlement_value_dollars"))  # YES-side $ (void mark)
        kalshi_settled = _kalshi_is_settled(market)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"fetch_settlement kalshi {kalshi_ticker}: {exc!r}")

    return classify(
        poly_price, is_short, kalshi_result, kalshi_side, poly_settled, kalshi_settled,
        kalshi_value=kalshi_value,
    )
