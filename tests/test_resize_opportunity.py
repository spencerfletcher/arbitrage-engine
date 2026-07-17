"""Fire-time re-sizing to what BOTH books can actually fill (S2).

Sizing happens at DETECTION off the WS cache and never knew the Kalshi book —
`_size_kalshi_opportunity` caps on Poly depth + balances only, on the false premise that
"Kalshi uses a market-maker model and fills any size at its quote". Kalshi is a CLOB; the repo's
own data says the book is EMPTY at our limit on 420/507 kalshi_moved events.

Kalshi depth was applied as a BINARY gate instead (thin_entry_depth): correct under FOK (no depth →
0 fill), obsolete under IOC — it rejects exactly the 87/507 (17%) partial-depth events the FOK→IOC
swap exists to capture. Resize instead, so both legs are sized to reality.

total_cost and guaranteed_profit are LINEAR in shares, so the resize is exact — but they MUST move
together: _record_hedge computes `ratio = qty/opp.shares` then `guaranteed_profit * ratio`, so
changing shares alone silently mis-books P&L.
"""
import pytest

from bot.kalshi.cross_arb import KalshiArbOpportunity, _effective_share_cost, resize_opportunity


def _opp(shares=10):
    poly_eff, kalshi_eff = _effective_share_cost(0.50, 0.06), 0.45
    return KalshiArbOpportunity(
        event_title="A vs B", poly_event_id="ev1", poly_token="POLY-A", poly_team="A",
        poly_ask_raw=0.50, poly_ask=poly_eff, kalshi_ticker="KX-AB-A", kalshi_side="no",
        kalshi_team="A", kalshi_ask=kalshi_eff, edge=1.0 - poly_eff - kalshi_eff, shares=shares,
        total_cost=shares * (poly_eff + kalshi_eff),
        guaranteed_profit=shares * 1.0 - shares * (poly_eff + kalshi_eff),
    )


def test_resize_keeps_per_share_economics_identical():
    """The point: shares change, the per-share edge does NOT. A resize must not invent or destroy
    edge — it only changes how much of the same edge we take."""
    o = _opp(10)
    pps_before = o.guaranteed_profit / o.shares
    cost_before = o.total_cost / o.shares
    resize_opportunity(o, 4)
    assert o.shares == 4
    assert o.guaranteed_profit / o.shares == pytest.approx(pps_before)
    assert o.total_cost / o.shares == pytest.approx(cost_before)


def test_resize_keeps_record_hedge_pnl_correct():
    """_record_hedge: ratio = qty/opp.shares; profit = guaranteed_profit * ratio. If shares moved
    and guaranteed_profit didn't, a full fill of the RESIZED order would book the ORIGINAL profit —
    a 2.5x overstatement here. This is why they must move together."""
    o = _opp(10)
    pps = o.guaranteed_profit / o.shares
    resize_opportunity(o, 4)
    booked = o.guaranteed_profit * (4 / o.shares)          # a full fill of the resized order
    assert booked == pytest.approx(4 * pps), "must book 4 shares' profit, not 10's"


def test_resize_never_grows_an_opportunity():
    """Only ever size DOWN to real depth. Growing would invent size no book supports."""
    o = _opp(8)
    resize_opportunity(o, 50)
    assert o.shares == 8


def test_resize_to_zero_or_negative_is_ignored_caller_rejects():
    """0 shares is not a trade — the caller rejects instead. Never produce a 0-share opp with
    0 cost and 0 profit that later reads as a real (free) position."""
    o = _opp(8)
    resize_opportunity(o, 0)
    assert o.shares == 8
    resize_opportunity(o, -3)
    assert o.shares == 8


def test_resize_is_a_noop_at_the_same_size():
    o = _opp(8)
    before = (o.shares, o.total_cost, o.guaranteed_profit)
    resize_opportunity(o, 8)
    assert (o.shares, o.total_cost, o.guaranteed_profit) == before
