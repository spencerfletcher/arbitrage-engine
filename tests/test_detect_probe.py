"""Detection probe: REST-verify an edge at FIRST SIGHT, before the 0.3s gate kills it (S11).

THE CATCH-22 IT BREAKS. would_fire / interleg / exec_order all log only for edges that SURVIVE the
persistence gate, so the data needed to judge the gate could only be collected by removing it. On
2026-07-15 the whole live slate produced 11 edges above the 2% floor and the gate killed 8 — every
probe logged zero rows. Waiting for more slates cannot fix that; it is structural.

We already know the gate does NOT filter phantoms (61.2% of its survivors are dead at the REST
read — a stale quote is maximally persistent, so it sails through a stability test). This measures
what it COSTS: of the edges it kills, how many were real at t=0?

Observability only. It never fires, never raises, never touches a gate.
"""
import types
from unittest.mock import AsyncMock

import pytest

from bot.runner import BotRunner
from bot.runner.kalshi_arb import (fire_limits, _loop_running, _kalshi_ask_from_book,
                                   _fillable_from_book)
from bot.kalshi.cross_arb import KalshiArbOpportunity, _effective_share_cost

_COLS = ["ts", "event", "ticker", "side", "shares", "ws_edge", "poly_ask_raw", "poly_limit",
         "t0_poly_ask", "t0_poly_state", "t0_poly_fill", "kalshi_ask", "kalshi_limit",
         "t0_entry_depth", "t0_rest_kalshi_ask", "t0_rest_ask_qty", "t0_ticker_err",
         "t0_poly_ok", "t0_kalshi_ok", "t0_real", "read_ms"]


def _opp():
    poly_eff = _effective_share_cost(0.26, 0.06)
    o = KalshiArbOpportunity(
        event_title="A vs B", poly_event_id="ev1", poly_token="tok", poly_team="A",
        poly_ask_raw=0.26, poly_ask=poly_eff, kalshi_ticker="KX-A", kalshi_side="no",
        kalshi_team="A", kalshi_ask=0.68, edge=0.0304, shares=7,
        total_cost=7 * (poly_eff + 0.70), guaranteed_profit=0.21)
    o.kalshi_tick = 0.01
    return o


def _kbook(kalshi_depth):
    """A REST orderbook (the venue's `orderbook_fp` shape) for the _opp() above, whose kalshi_limit
    floors to 0.72 → buying `no` lifts yes bids at px >= 1-0.72 = 0.28.

    depth>0 → one yes bid at 0.32 (inside our limit) carrying that depth.
    depth==0 → a REAL book priced AWAY from our limit (yes bid 0.20 ⇒ the no side offered at 0.80,
      against a ticker claiming 0.68). That is the actual phantom — a stale-LOW ticker over a live
      book — not an empty array, which would model a market with no orders at all. The distinction
      matters: `_kalshi_ask_from_book` reports None/0.0 for the empty case and a real 0.80 here, and
      only the latter can measure the ticker's error."""
    px = "0.32" if kalshi_depth else "0.20"
    qty = str(kalshi_depth) if kalshi_depth else "500"
    return {"orderbook_fp": {"yes_dollars": [[px, qty]], "no_dollars": [["0.66", "9"]]}}


async def _probe(kalshi_depth, poly_ask, state, book="auto"):
    r = BotRunner.__new__(BotRunner)
    r._poly_us = True
    rows = []
    r._detect_probe_csv = types.SimpleNamespace(writerow=rows.append)
    # Mocks _rest_book, NOT _rest_fillable: the probe reads ONE snapshot and derives both the depth
    # and the book's own best ask from it. Mocking the old seam would let the two diverge here in a
    # way production cannot, and the whole point of the ask column is that they share a book.
    r._rest_book = AsyncMock(return_value=_kbook(kalshi_depth) if book == "auto" else book)
    r._poly_fill_quote = AsyncMock(
        return_value=((poly_ask, state, [(poly_ask, 500.0)], None, {}), 12.0))
    await r._probe_edge_at_detection(_opp())
    if not rows:
        return None
    # _COLS mirrors the header in runner.py. zip() truncates silently, so a width drift would
    # misalign every assertion below onto the wrong column and still pass. Fail loudly instead.
    assert len(rows[0]) == len(_COLS), (
        f"detect_probe row has {len(rows[0])} fields, _COLS has {len(_COLS)} — "
        "the writer and runner.py's header have drifted apart")
    return dict(zip(_COLS, rows[0]))


@pytest.mark.asyncio
async def test_a_real_edge_reads_real():
    m = await _probe(500, 0.26, "MARKET_STATE_OPEN")
    assert (m["t0_poly_ok"], m["t0_kalshi_ok"], m["t0_real"]) == (1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("label,depth,ask,state", [
    ("kalshi book empty at our limit", 0, 0.26, "MARKET_STATE_OPEN"),
    ("poly ask moved past our limit", 500, 0.40, "MARKET_STATE_OPEN"),
    ("poly suspended (goal halt)", 500, 0.26, "MARKET_STATE_SUSPENDED"),
])
async def test_each_phantom_mode_reads_not_real(label, depth, ask, state):
    """t0_real must require what the FIRE PATH requires — Poly OPEN and takeable at our limit,
    Kalshi with >=1 contract at ours. If it were laxer, killed phantoms would read as recoverable
    and argue for removing a gate that is doing its job."""
    m = await _probe(depth, ask, state)
    assert m["t0_real"] == 0, label


# ── t0_rest_kalshi_ask: measure the ticker's error, don't infer it ────────────────────────────

# CAPTURED VERBATIM from the live venue 2026-07-16 (read-only GET
# /markets/KXMLBGAME-26JUL172210SFSEA-SF/orderbook, SF@SEA, pre-game). NOT hand-written: the
# fixtures below it are shape-models, which can only ever agree with the code that they were
# written from. Things this recorded response knows that a model would not:
#   • quantities are FRACTIONAL ("326.89") — contracts look integral; they are not
#   • levels arrive ASCENDING, so the BEST bid is the LAST element, not the first
#   • prices are 4-dp strings ("0.3600"), quantities 2-dp strings — both str, neither float
#   • the ladder is DEEPER than any single read: depth=100 on this market returned 27 yes / 45 no.
#     This fixture has 10 because the capture call used get_orderbook's default; the FIRE PATH
#     passes depth=20 (_rest_book). Level count here is an artifact of the capture, not a venue
#     fact — do not read "10" off this fixture as what production sees.
_REAL_BOOK = {"orderbook_fp": {
    "no_dollars": [["0.3600", "1.00"], ["0.3900", "1.00"], ["0.4300", "1.00"], ["0.4700", "2081.00"],
                   ["0.5000", "1307.00"], ["0.5200", "1155.00"], ["0.5300", "7855.00"],
                   ["0.5400", "5302.00"], ["0.5600", "883.00"], ["0.5700", "326.89"]],
    "yes_dollars": [["0.2600", "6.00"], ["0.2700", "1.00"], ["0.3000", "5.00"], ["0.3200", "2117.00"],
                    ["0.3500", "1220.00"], ["0.3700", "5911.00"], ["0.3800", "5100.00"],
                    ["0.3900", "1213.00"], ["0.4000", "3081.00"], ["0.4100", "362.00"]]}}


def test_ask_from_book_against_a_REAL_captured_book():
    """The polarity, pinned against a response nobody wrote.

    THE CHECK THAT MAKES THIS EVIDENCE — a side's ASK must sit just above that SAME side's own
    BID, because both describe one side and the gap between them IS the spread. That is a venue
    property, independent of our arithmetic, and it is what catches a polarity error:
      correct        yes_bid 0.41 → yes_ask 0.43   (2c apart ✓)
      side inverted  yes_bid 0.41 → yes_ask 0.59   (18c apart — that's the NO price ✗)
      min-for-max    yes_bid 0.41 → yes_ask 0.64   (23c ✗)

    ⚠️ Do NOT use "the two asks sum above 1.00" for this. An earlier version of this test claimed
    that check caught side inversion. It does not: inverting swaps 0.43/0.59 → 0.59/0.43, which
    still sums to 1.02 and passes. (It catches min-for-max only, and by going ABOVE 1.10 — not
    "below 1.00" as that comment also asserted. Both halves were wrong; a mutation run caught it.)
    Kept below as a weak book-sanity assert, correctly labelled.

    Market context, for the reader: `-SF` is "SF wins". yes_dollars are bids for SF (best 0.41),
    no_dollars bids against (best 0.57) ⇒ the market prices SF ≈ 0.41–0.43.
    """
    yes_ask, yes_qty = _kalshi_ask_from_book(_REAL_BOOK, "yes")
    no_ask, no_qty = _kalshi_ask_from_book(_REAL_BOOK, "no")
    assert (yes_ask, no_ask) == (pytest.approx(0.43), pytest.approx(0.59))
    for side, ask in (("yes", yes_ask), ("no", no_ask)):
        own_bid = max(float(p) for p, _ in _REAL_BOOK["orderbook_fp"][f"{side}_dollars"])
        assert own_bid < ask <= own_bid + 0.05, (
            f"{side}: ask {ask} vs its own best bid {own_bid} — an uncrossed book puts the ask one "
            "spread ABOVE the same side's bid; a large gap means the side or the max/min inverted")
    # Weak sanity only (does NOT catch side inversion): a two-sided book's asks exceed 1 by the spread.
    assert 1.0 < yes_ask + no_ask < 1.10
    # Depth at the touch, fractional as the venue actually sends it.
    assert (yes_qty, no_qty) == (pytest.approx(326.89), pytest.approx(362.0))


def test_fractional_quantities_survive_the_parse():
    """'326.89' — a real level. Any int() on the quantity path truncates real depth silently."""
    _, qty = _kalshi_ask_from_book(_REAL_BOOK, "yes")
    assert qty == pytest.approx(326.89)
    assert _fillable_from_book(_REAL_BOOK, "yes", 0.99) == pytest.approx(
        sum(float(q) for _, q in _REAL_BOOK["orderbook_fp"]["no_dollars"]))


def test_ask_from_book_inverts_the_side_like_fillable_does():
    """Buying `side` lifts the OPPOSING book's bids, so best_ask = 1 - max(opposing bid) — the
    HIGHEST bid is the CHEAPEST offer. Reading the side's own array (or min instead of max) would
    report a plausible wrong price, which is worse than none: the whole column exists to be
    subtracted from the ticker."""
    book = {"orderbook_fp": {
        "yes_dollars": [["0.32", "100"], ["0.30", "50"], ["0.20", "999"]],
        "no_dollars":  [["0.66", "9"], ["0.60", "4"]],
    }}
    # Buying NO reads yes bids: best yes bid 0.32 → NO offered at 0.68, 100 contracts there.
    assert _kalshi_ask_from_book(book, "no") == (pytest.approx(0.68), 100.0)
    # Buying YES reads no bids: best no bid 0.66 → YES offered at 0.34, 9 contracts.
    assert _kalshi_ask_from_book(book, "yes") == (pytest.approx(0.34), 9.0)


def test_ask_from_book_is_THREE_state_not_collapsed_to_zero():
    """(None, None) = we never read a book; (None, 0.0) = we read one with no offers. _fillable_
    from_book collapses both to 0.0 because unknown MUST reject on the fire path — here that
    collapse would make an unread book indistinguishable from an empty one in the log, which is
    the exact ambiguity data_regimes.md already has to warn about for kalshi_fillable."""
    assert _kalshi_ask_from_book({"orderbook_fp": {"yes_dollars": []}}, "no") == (None, 0.0)
    for unread in ({}, {"orderbook_fp": None}, {"orderbook_fp": {"yes_dollars": "nope"}},
                   "not a dict", None):
        assert _kalshi_ask_from_book(unread, "no") == (None, None), unread


def test_ask_from_book_never_raises_and_skips_junk_levels():
    """Rides the probe's outer except, but a raise here would cost the whole row. A dict-shaped
    level throws KeyError on lvl[0] — the shape that already breached an enumerated except once."""
    book = {"orderbook_fp": {"yes_dollars": [["x", "y"], {"a": 1}, ["0.32", "7"]]}}
    assert _kalshi_ask_from_book(book, "no") == (pytest.approx(0.68), 7.0)


@pytest.mark.asyncio
async def test_it_records_HOW_WRONG_the_ticker_was_not_just_that_it_was():
    """The phantom: ticker claims NO at 0.68, the real book only offers it at 0.80. t0_entry_depth
    alone reports 0 — 'wrong, by an unknown amount'. This pins the magnitude, which is the claim
    docs/TODO.md's orderbook item actually makes."""
    m = await _probe(0, 0.26, "MARKET_STATE_OPEN")     # book priced away — see _kbook
    assert m["t0_entry_depth"] == "0"
    assert float(m["t0_rest_kalshi_ask"]) == pytest.approx(0.80)
    # +0.12 = ticker quoted 12c CHEAPER than the book sells → the phantom-manufacturing direction.
    assert float(m["t0_ticker_err"]) == pytest.approx(0.12)
    assert m["t0_ticker_err"].startswith("+"), "sign must be explicit: stale-LOW vs stale-HIGH"


@pytest.mark.asyncio
async def test_a_backed_ticker_shows_no_error():
    """The control. If a healthy quote also logged a fat error, the column would be measuring our
    own arithmetic rather than the venue."""
    m = await _probe(500, 0.26, "MARKET_STATE_OPEN")
    assert float(m["t0_ticker_err"]) == pytest.approx(0.0)
    assert float(m["t0_rest_kalshi_ask"]) == pytest.approx(0.68)   # == opp.kalshi_ask


@pytest.mark.asyncio
async def test_an_unread_book_logs_BLANK_never_zero():
    """_rest_book returns None only when the read RAISED. A 0 here would read as 'the ticker was
    perfect' — inventing evidence against the very hypothesis this probe tests."""
    m = await _probe(0, 0.26, "MARKET_STATE_OPEN", book=None)
    assert m["t0_rest_kalshi_ask"] == "" and m["t0_ticker_err"] == "" and m["t0_rest_ask_qty"] == ""
    assert m["t0_entry_depth"] == "0", "depth still fails CLOSED — unknown must not read as real"
    assert m["t0_kalshi_ok"] == 0


@pytest.mark.asyncio
async def test_it_uses_the_SAME_limits_the_fire_path_would():
    """A probe that derives limits its own way measures a bot we don't run."""
    m = await _probe(500, 0.26, "MARKET_STATE_OPEN")
    poly_limit, kalshi_limit = fire_limits(_opp())
    assert float(m["poly_limit"]) == pytest.approx(poly_limit)
    assert float(m["kalshi_limit"]) == pytest.approx(kalshi_limit)


@pytest.mark.asyncio
async def test_it_never_raises_into_detection():
    """It hangs off _confirm_persistent — a SYNC gate. A raise here would take out the gate."""
    r = BotRunner.__new__(BotRunner)
    r._poly_us = True
    r._detect_probe_csv = types.SimpleNamespace(writerow=lambda *_: (_ for _ in ()).throw(IOError()))
    r._rest_fillable = AsyncMock(side_effect=RuntimeError("boom"))
    r._poly_fill_quote = AsyncMock(side_effect=RuntimeError("boom"))
    await r._probe_edge_at_detection(_opp())          # must not raise


def test_loop_guard_is_false_without_a_running_loop():
    """_confirm_persistent is sync and unit-called without a loop. Checking BEFORE building the
    coroutine matters: create_task(coro()) constructs coro() first, so guarding the create_task
    alone leaks a 'never awaited' coroutine."""
    assert _loop_running() is False


@pytest.mark.asyncio
async def test_loop_guard_is_true_inside_a_loop():
    assert _loop_running() is True


def test_the_FIRE_PATH_actually_calls_fire_limits():
    """fire_limits' whole reason to exist, asserted structurally.

    Its docstring promises "a probe that derives limits its own way measures a bot we don't run".
    But the extraction only half-landed: the probe was repointed at it and `_execute_kalshi_arb`
    kept re-deriving all three lines inline, byte-identical. That did not close the drift vector —
    it REVERSED it. The probe held the copy, and its output feeds detect_probe.csv and the S11
    decision on whether to delete the 0.3s persistence gate, so changing the fire path's prices
    would have left that decision resting on the old bot's.

    Byte-identical copies pass every value test, so pin the CALL. This is the only thing that
    notices someone inlining it again.
    """
    import ast
    import bot.runner.kalshi_arb as kexec

    src = open(kexec.__file__).read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_execute_kalshi_arb")

    calls = {getattr(c.func, "id", "") for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "fire_limits" in calls, "_execute_kalshi_arb must derive its limits from fire_limits"
    # ...and must NOT re-derive them itself.
    assert "_fok_buffer" not in calls, "the buffer belongs to fire_limits, not the fire path"
    assert "_kalshi_breakeven_ask" not in calls, "the breakeven belongs to fire_limits"


def test_the_probe_and_the_fire_path_cannot_disagree_by_construction():
    """Both read the same function, so this is a tautology today — which is exactly the state we
    want, and the test above is what keeps it true."""
    import ast
    import bot.runner.kalshi_arb as kexec
    tree = ast.parse(open(kexec.__file__).read())
    users = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and any(getattr(c.func, "id", "") == "fire_limits"
                     for c in ast.walk(n) if isinstance(c, ast.Call))}
    assert {"_execute_kalshi_arb", "_probe_edge_at_detection"} <= users, (
        f"both the fire path and the detect probe must read fire_limits; got {users}")
