"""Exec-order probe: unwind-cost + kalshi_first-mirror measurement (DRY-only observability).

Feeds the poly_first-vs-kalshi_first decision. The decision is NOT "what does a Kalshi unwind
cost" — it is `P(kalshi kills) x poly_flatten` vs `P(poly kills) x kalshi_unwind` (64.0% vs 5.9%
on 792 economic events), so BOTH costs must be modelled the same way the REAL unwind paths price
them, or the comparison is fiction. These pin the two book-reading helpers, where a side/sign
error would silently invert the answer.
"""
import pytest

from bot.runner.kalshi_arb import _poly_exit_from_book, _sell_fillable_from_book


# ── Kalshi sell-side depth: can the unwind actually GET OUT? ──────────────────────────────────
def test_sell_fillable_reads_the_SIDE_OWN_bids_not_the_opposite_book():
    """The mirror of _fillable_from_book, and BOTH the array and the comparison invert:
        BUY  `side` lifts the OPPOSING book's bids → `{opposite}_dollars`, px >= 1-limit
        SELL `side` hits `side`'s OWN bids         → `{side}_dollars`,     px >= min_price
    Reading the opposite array reports the WRONG leg's depth — silently."""
    book = {"orderbook_fp": {
        "yes_dollars": [["0.60", "100"], ["0.55", "50"], ["0.40", "999"]],
        "no_dollars":  [["0.30", "7"]],
    }}
    # Selling YES at >= 0.55 → yes bids at 0.60 and 0.55 → 150. The 0.40 bid is too low.
    assert _sell_fillable_from_book(book, "yes", 0.55) == 150.0
    # Selling NO reads no_dollars — must NOT pick up the (much deeper) yes side.
    assert _sell_fillable_from_book(book, "no", 0.30) == 7.0


def test_sell_fillable_excludes_bids_below_our_sell_price():
    book = {"orderbook_fp": {"yes_dollars": [["0.20", "500"]]}}
    assert _sell_fillable_from_book(book, "yes", 0.55) == 0.0


def test_sell_fillable_never_raises_on_junk():
    """Observability on the fire path — must never raise, and garbage levels are skipped."""
    for junk in ({}, {"orderbook_fp": None}, {"orderbook_fp": {"yes_dollars": "nope"}},
                 "not a dict", {"orderbook_fp": {"yes_dollars": [["x", "y"], ["0.60", "5"]]}}):
        assert isinstance(_sell_fillable_from_book(junk, "yes", 0.5), float)
    assert _sell_fillable_from_book(
        {"orderbook_fp": {"yes_dollars": [["x", "y"], ["0.60", "5"]]}}, "yes", 0.5) == 5.0


# ── Poly exit price: must match how sell_back ACTUALLY picks its price ────────────────────────
def _md(bids=(), offers=()):
    return {"bids":   [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in bids],
            "offers": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in offers]}


def test_poly_exit_long_sells_at_the_BEST_BID():
    """sell_back (long) → SELL_LONG at max(bids). The modelled cost must use the same price the
    real unwind would use, or the measurement is a proxy rather than the answer."""
    px, depth = _poly_exit_from_book(_md(bids=[(0.60, 10), (0.55, 40)]), is_short=False)
    assert px == pytest.approx(0.60)
    assert depth == 10.0            # depth AT the best bid


def test_poly_exit_short_sells_at_one_minus_the_BEST_ASK():
    """sell_back (short) → SELL_SHORT at 1 − min(offers). A sign error here inverts the cost."""
    px, depth = _poly_exit_from_book(_md(offers=[(0.40, 25), (0.45, 5)]), is_short=True)
    assert px == pytest.approx(0.60)   # 1 − 0.40 (the BEST/lowest long ask)
    assert depth == 25.0


def test_poly_exit_long_ignores_the_offers_side():
    """A long exit must not read the offers (that's the BUY side) — it would price the unwind at
    the ask and report a profit on a flatten."""
    px, _ = _poly_exit_from_book(_md(bids=[(0.30, 5)], offers=[(0.90, 500)]), is_short=False)
    assert px == pytest.approx(0.30)


def test_poly_exit_no_liquidity_is_none_not_zero():
    """Nothing to sell into → (None, 0.0). A 0.0 price would book a 100%-loss flatten as if real;
    None says 'unknown' and the probe logs BLANK (blank≠zero, project-wide)."""
    assert _poly_exit_from_book(_md(), is_short=False) == (None, 0.0)
    assert _poly_exit_from_book({}, is_short=False) == (None, 0.0)


def test_poly_exit_never_raises_on_junk():
    for junk in ({"bids": "nope"}, {"bids": [{"no_px": 1}]}, {"bids": [{"px": None}]}):
        px, depth = _poly_exit_from_book(junk, is_short=False)
        assert px is None and depth == 0.0


# ── the cost each helper implies (the numbers the decision turns on) ──────────────────────────
def test_modelled_kalshi_unwind_cost_matches_the_real_unwind_formula():
    """_unwind_kalshi_excess sells IOC at max(0.01, tick_floor(bid − 0.02)) and books
    (kalshi_ask − sell_price) x qty. The probe must reproduce that exactly."""
    from bot.kalshi.cross_arb import kalshi_tick_floor
    kalshi_ask, bid, tick = 0.45, 0.43, 0.01
    sell = max(0.01, kalshi_tick_floor(bid - 0.02, tick))
    assert sell == pytest.approx(0.41)
    assert (kalshi_ask - sell) == pytest.approx(0.04)   # 4¢/share to unwind — spread + the 2¢


# ── S14: a null orderbook_fp must not RAISE into the fire path ────────────────────────────────
def test_fillable_from_book_never_raises_on_a_null_orderbook():
    """`book.get("orderbook_fp")` returning None (key PRESENT, value null) made `.get` on None throw
    AttributeError. Both fire-path callers parse OUTSIDE their try — `_rest_fillable`'s guard covers
    get_orderbook, not the parse — so it escaped into _execute_kalshi_arb, and at ~938 into
    _log_reject (a LOGGING path taking down a decision path).

    Fails closed (0.0 → not fillable → reject), which is the SAME direction as an empty book. That is
    correct here: this is the fire path, where 'unknown depth' must never fire. (The §5 probe is the
    opposite case — there 0.0 manufactures a kill, which is why it gates on _book_levels_present.)
    """
    from bot.runner.kalshi_arb import _fillable_from_book
    for junk in ({"orderbook_fp": None},              # the crash: key present, value null
                 {"orderbook_fp": {"yes_dollars": None}},
                 {"orderbook_fp": "nope"},
                 {"error": "market_paused"},
                 None, "not a dict", 42):
        assert _fillable_from_book(junk, "no", 0.60) == 0.0


# ── Kalshi exit: the STRUCTURAL mirror of _poly_exit_from_book ────────────────────────────────
from bot.runner.kalshi_arb import _kalshi_exit_from_book


def test_kalshi_exit_sells_at_the_BEST_BID_with_the_depth_AT_it():
    """(best bid, depth at that price) — not the sum of the ladder, matching _poly_exit_from_book."""
    book = {"orderbook_fp": {
        "yes_dollars": [["0.43", "60"], ["0.44", "25"], ["0.42", "999"]],
        "no_dollars":  [["0.30", "7"]],
    }}
    px, depth = _kalshi_exit_from_book(book, "yes")
    assert px == pytest.approx(0.44)    # highest bid — selling wants the MOST offered
    assert depth == 25.0                # depth AT 0.44 only; 0.43/0.42 sit below it
    # Reads the side's OWN bids — must not pick up the other array.
    assert _kalshi_exit_from_book(book, "no") == (pytest.approx(0.30), 7.0)


def test_kalshi_exit_derives_its_price_from_the_book_not_a_passed_threshold():
    """The bug this function exists to prevent.

    _sell_fillable_from_book takes a min_price, so pricing the sampler's depth off the WS feed's
    bid compared two TRANSPORTS against one `>=`. When they disagree by a tick — routine, they are
    different transports read at different instants — a WS bid one tick ABOVE the REST book's best
    silently returns 0.0, indistinguishable in the log from "nothing to sell into".
    """
    book = {"orderbook_fp": {"yes_dollars": [["0.43", "60"]]}}
    # The old shape: WS says 0.44, book's best is 0.43 → reads as no depth at all.
    assert _sell_fillable_from_book(book, "yes", 0.44) == 0.0
    # The structural read is immune — it never consults an outside price.
    assert _kalshi_exit_from_book(book, "yes") == (pytest.approx(0.43), 60.0)


def test_kalshi_exit_skips_garbage_levels_but_still_reads_the_good_ones():
    # A level that won't parse is dropped, not fatal, and doesn't suppress the rest of the ladder.
    # (The unreadable-vs-empty split is pinned separately, below.)
    assert _kalshi_exit_from_book(
        {"orderbook_fp": {"yes_dollars": [["x", "y"], ["0.60", "5"]]}}, "yes"
    ) == (pytest.approx(0.60), 5.0)


def test_kalshi_exit_splits_UNREADABLE_from_genuinely_empty():
    """(None, None) = we never read a book; (None, 0.0) = we read, nothing to sell into.

    _fillable_from_book collapses both into 0.0 because on the fire path unknown MUST reject. In a
    log that collapse is a lie: a 200 with an error body or a changed shape would record "the book
    was empty", a claim about the market rather than about our read.
    """
    for unreadable in ("not a dict", {}, {"orderbook_fp": None}, {"orderbook_fp": "nope"},
                       {"orderbook_fp": {"yes_dollars": None}}, {"orderbook_fp": {}}):
        assert _kalshi_exit_from_book(unreadable, "yes") == (None, None), unreadable
    # Read it, and it was empty — a real observation, and a different cell value.
    assert _kalshi_exit_from_book({"orderbook_fp": {"yes_dollars": []}}, "yes") == (None, 0.0)


def test_kalshi_exit_never_raises_on_a_dict_shaped_level():
    """A KeyError here would cost the WHOLE sample row, not just these two cells.

    The level loop indexes lvl[0]/lvl[1]. A dict-shaped level raises KeyError, which the sibling
    readers' (TypeError, ValueError, IndexError) tuple does NOT catch — it would escape into
    _sample_book_evolution's bare except and drop every column in the row.
    """
    book = {"orderbook_fp": {"yes_dollars": [
        {"price_dollars": "0.40", "quantity": 5},   # dict-shaped → KeyError on lvl[0]
        ["0.60", "100"],                            # good level, must survive
    ]}}
    assert _kalshi_exit_from_book(book, "yes") == (pytest.approx(0.60), 100.0)


# ── NEVER RAISES is load-bearing: these run OUTSIDE their callers' try ────────────────────────
# _rest_fillable's guard covers get_orderbook, not the parse, so a raise reaches the FIRE PATH;
# _kalshi_rest_depth_str runs inside _log_reject, where a logging path could kill a decision path.
# A dict-shaped level throws KeyError on lvl[0], which the old (TypeError, ValueError, IndexError)
# tuple did not catch — the level parse now catches Exception so the guarantee is structural.
from bot.runner.kalshi_arb import _fillable_from_book, _kalshi_ask_levels

_DICT_LEVEL = {"price_dollars": "0.40", "quantity": 5}     # the shape that breached it


@pytest.mark.parametrize("reader,args,expect", [
    (_fillable_from_book,      ("no", 0.45), 5.0),    # yes_dollars @0.60 >= 1-0.45 → 5
    (_sell_fillable_from_book, ("yes", 0.5), 5.0),    # yes_dollars @0.60 >= 0.50   → 5
])
def test_book_readers_never_raise_on_a_dict_shaped_level(reader, args, expect):
    book = {"orderbook_fp": {"yes_dollars": [_DICT_LEVEL, ["0.60", "5"]]}}
    assert reader(book, *args) == expect      # bad level skipped, good one still counted


def test_kalshi_ask_levels_never_raises_on_a_dict_shaped_level():
    book = {"orderbook_fp": {"yes_dollars": [_DICT_LEVEL, ["0.60", "5"]]}}
    assert _kalshi_ask_levels(book, "no") == [(pytest.approx(0.40), 5.0)]   # 1 − 0.60


def test_book_readers_never_raise_on_any_malformed_level():
    """A guarantee spelled as a type list only holds for the shapes we thought of; the venue
    supplies the shapes. Every reader must survive anything in the levels array."""
    junk = [_DICT_LEVEL, None, 42, [], ["a"], ["a", "b"], set(), object(), {"0": 1}]
    for lvl in junk:
        book = {"orderbook_fp": {"yes_dollars": [lvl], "no_dollars": [lvl]}}
        assert _fillable_from_book(book, "no", 0.45) == 0.0
        assert _sell_fillable_from_book(book, "yes", 0.5) == 0.0
        assert _kalshi_ask_levels(book, "no") == []
