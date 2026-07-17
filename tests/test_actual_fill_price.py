"""Book P&L off the ACTUAL fill, not the detection price (S3).

_record_hedge books `opp.poly_ask`/`opp.kalshi_ask` — the DETECTION-time effective costs — and
writes that into trades.log's `cost`. settlement_scorer.py:129 reads that same `cost` to compute
realized_settled, which CLAUDE.md calls "the INTENDED sizing authority" and which the cumulative
loss cap reads. So a fill worse than detection doesn't merely flatter marked_unsettled; it corrupts
the number that will authorize scaling up.

Both venues report the real cost exactly — price AND fee — so no modelling is needed:
  Kalshi V2 create: average_fill_price + average_fee_paid   [real prod fixture]
  Poly create:      avgPx + commissionNotionalTotalCollected [real prod fixture]
"""
import pytest

from bot.kalshi.client import kalshi_avg_fill_cost
from bot.kalshi.cross_arb import _kalshi_taker_fee
from bot.poly_us.client import poly_avg_fill_cost


# The REAL prod V2 create response (1 YES @ 0.4270, fee $0.0172) — same fixture as
# tests/test_kalshi_v2_orders.py. Not invented.
REAL_V2_CREATE = {
    "order_id": "00000000-0000-4000-8000-000000000000", "fill_count": "1.00",
    "remaining_count": "0.00", "average_fill_price": "0.4270",
    "average_fee_paid": "0.0172", "ts_ms": 1784063018859,
}


def test_kalshi_actual_cost_is_price_plus_the_REAL_fee():
    """0.4270 + 0.0172/1 = 0.4442 — the exchange's own numbers, no fee model involved."""
    assert kalshi_avg_fill_cost(REAL_V2_CREATE) == pytest.approx(0.4442)


def test_kalshi_fee_basis_is_DETECTED_not_assumed():
    """We do not know whether average_fee_paid is per-contract or per-order, so we don't guess.

    It cannot be measured read-only — the field exists on the create response and nowhere else
    (/portfolio/fills has fee_cost, /portfolio/orders has taker_fees_dollars; neither is this).
    Our only real fixture is fill_count=1, where the two bases are arithmetically IDENTICAL, so
    the measurement that "verified" this could never have discriminated. The predecessor of this
    test asserted per-ORDER using `4 @ 0.50 → fee 0.0400` — a value the venue cannot produce (the
    real order total is 0.0700, the real per-contract 0.0175). It pinned the code's own arithmetic
    against an impossible world.

    The fee FORMULA is verified to the centicent against 7 real fills, and the two candidates
    differ by a factor of fill_count, so the response identifies itself. The point of this test:
    **fed a REAL fee, both bases yield the same cost**, so the answer no longer depends on the
    guess. Getting it wrong is not cheap either way — reading per-contract as a total understates
    by ~1.5c/share at the 8-share ramp (77% of the minimum edge, flattering realized_settled),
    and the mirror overstates by ~12c/share, which would trip the cumulative loss cap.
    """
    per_contract_fee = _kalshi_taker_fee(0.50, 10)          # 0.0175
    order_total_fee = per_contract_fee * 10                 # 0.1750

    as_per_contract = dict(REAL_V2_CREATE, fill_count="10.00", average_fill_price="0.5000",
                           average_fee_paid=f"{per_contract_fee:.6f}")
    as_order_total = dict(REAL_V2_CREATE, fill_count="10.00", average_fill_price="0.5000",
                          average_fee_paid=f"{order_total_fee:.6f}")

    assert kalshi_avg_fill_cost(as_per_contract) == pytest.approx(0.5175)
    assert kalshi_avg_fill_cost(as_order_total) == pytest.approx(0.5175)


def test_kalshi_fee_matching_neither_basis_refuses_to_price_the_leg():
    """A fee that is neither candidate means the schedule moved or the field changed. Returning a
    number would price a real position off arithmetic we no longer understand; None makes the
    caller fall back explicitly. This is what caught the two fabricated fixtures in this suite."""
    impossible = dict(REAL_V2_CREATE, fill_count="10.00", average_fill_price="0.5000",
                      average_fee_paid="0.1000")   # neither 0.0175 nor 0.1750
    assert kalshi_avg_fill_cost(impossible) is None


def test_kalshi_n1_is_unaffected_by_the_basis_question():
    """The real prod fill: at fill_count=1 the two bases coincide, so this stays exactly as it
    was measured — 0.4270 + 0.0172."""
    assert kalshi_avg_fill_cost(REAL_V2_CREATE) == pytest.approx(0.4442)


def test_kalshi_unreadable_returns_None_never_a_price():
    """None = "use the caller's fallback". A 0.0 here would book a FREE fill — the most
    flattering possible lie about a leg we actually paid for."""
    assert kalshi_avg_fill_cost({"fill_count": "0.00"}) is None
    assert kalshi_avg_fill_cost({"average_fill_price": "0.4"}) is None      # no fill_count
    assert kalshi_avg_fill_cost({"fill_count": "1.00"}) is None             # no price
    assert kalshi_avg_fill_cost(None) is None
    assert kalshi_avg_fill_cost({"fill_count": "1.00", "average_fill_price": "x"}) is None


def test_kalshi_missing_fee_is_unreadable_not_free():
    """A price with no fee must NOT book as fee-free — that silently under-costs the leg, the
    exact failure the Poly fee fallback exists to prevent ("NEVER 0")."""
    assert kalshi_avg_fill_cost({"fill_count": "1.00", "average_fill_price": "0.4270"}) is None


def _poly_resp(cum, avg_px, commission):
    return {"executions": [{"order": {
        "cumQuantity": cum, "avgPx": {"value": avg_px, "currency": "USD"},
        "commissionNotionalTotalCollected": {"value": commission, "currency": "USD"}}}]}


def test_poly_actual_cost_is_avgpx_plus_the_REAL_commission():
    """255 @ 0.0090 with $0.1400 commission — the real 2026-07-15 probe fill.
    0.0090 + 0.1400/255 = 0.009549."""
    assert poly_avg_fill_cost(_poly_resp(255, "0.0090", "0.1400")) == pytest.approx(0.009549, abs=1e-6)


def test_poly_reads_the_execution_that_actually_FILLED():
    """executions[0] LIES (verified: cumQuantity 0 on an order that filled 255). Take the max-fill
    execution, same rule as order_filled_qty."""
    r = {"executions": [
        {"order": {"cumQuantity": 0, "avgPx": {"value": "0"},
                   "commissionNotionalTotalCollected": {"value": "0"}}},
        {"order": {"cumQuantity": 255, "avgPx": {"value": "0.0090"},
                   "commissionNotionalTotalCollected": {"value": "0.1400"}}},
    ]}
    assert poly_avg_fill_cost(r) == pytest.approx(0.009549, abs=1e-6)


def test_poly_unreadable_returns_None_never_a_price():
    assert poly_avg_fill_cost({"executions": []}) is None
    assert poly_avg_fill_cost(None) is None
    assert poly_avg_fill_cost(_poly_resp(0, "0.5", "0.1")) is None            # nothing filled
    assert poly_avg_fill_cost({"executions": [{"order": {"cumQuantity": 5}}]}) is None  # no px


# ── S3's other half: the UNWIND paths ─────────────────────────────────────────────────────────
# S3 fixed _record_hedge to book the venues' own fill reports and stopped there. The unwinds kept
# booking detection prices into execution_pnl.csv, which safety.is_exec_cost_cap_hit reads — so
# the lifetime loss ratchet was fed an optimistic number.

def test_kalshi_sell_proceeds_NET_the_fee_they_do_not_add_it():
    """A buy PAYS the fee, a sell NETS it. Reading a sell with the buy helper would report
    proceeds two fees too high and make every unwind look cheaper than it was."""
    from bot.kalshi.client import kalshi_avg_sell_proceeds
    fee_pc = _kalshi_taker_fee(0.40, 10)
    resp = {"fill_count": "10.00", "average_fill_price": "0.4000",
            "average_fee_paid": f"{fee_pc * 10:.6f}"}          # reported as an order total
    assert kalshi_avg_sell_proceeds(resp) == pytest.approx(0.40 - fee_pc)
    assert kalshi_avg_fill_cost(resp) == pytest.approx(0.40 + fee_pc)
    # ...and the gap between them is exactly two fees — what the mistake would have cost.
    assert kalshi_avg_fill_cost(resp) - kalshi_avg_sell_proceeds(resp) == pytest.approx(2 * fee_pc)


def test_kalshi_sell_proceeds_resolve_the_fee_basis_like_the_buy_side():
    """Same unknown basis, same resolver — answering it twice would let the two drift."""
    from bot.kalshi.client import kalshi_avg_sell_proceeds
    fee_pc = _kalshi_taker_fee(0.40, 10)
    as_pc = {"fill_count": "10.00", "average_fill_price": "0.4000",
             "average_fee_paid": f"{fee_pc:.6f}"}
    as_total = {"fill_count": "10.00", "average_fill_price": "0.4000",
                "average_fee_paid": f"{fee_pc * 10:.6f}"}
    assert kalshi_avg_sell_proceeds(as_pc) == pytest.approx(kalshi_avg_sell_proceeds(as_total))
    # neither basis → refuse, same as the buy side
    assert kalshi_avg_sell_proceeds(
        {"fill_count": "10.00", "average_fill_price": "0.4000",
         "average_fee_paid": "0.1000"}) is None


def test_kalshi_effective_is_NOT_opp_kalshi_ask():
    """The naming trap that made the fallback fee-free.

        opp.poly_ask     -> EFFECTIVE (the Poly fee is already in it)
        opp.kalshi_ask   -> RAW       (the fee is NOT)
        opp.poly_ask_raw -> raw (the one whose name says so)

    Reading the two `*_ask` fields symmetrically drops ~1.7c/share — against a 2c minimum edge,
    ~85% of it. This is the same arithmetic check_kalshi_arb uses to compute the edge, so booking
    less is not a detection-price fallback, it is a fee-free one.
    """
    import types
    from bot.runner.kalshi_arb import _kalshi_effective
    opp = types.SimpleNamespace(kalshi_ask=0.45)
    assert _kalshi_effective(opp) == pytest.approx(0.45 + _kalshi_taker_fee(0.45))
    assert _kalshi_effective(opp) > opp.kalshi_ask
    # the gap is material against the 2% floor
    assert (_kalshi_effective(opp) - opp.kalshi_ask) > 0.015
