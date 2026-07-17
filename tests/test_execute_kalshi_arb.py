"""Execution-path tests for BotRunner._execute_kalshi_arb (concurrent both-IOC).

Builds a runner via __new__ with mocked clients + a real PositionTracker (temp
state file), and asserts the leg-reconciliation invariant: a hedged pair is
recorded, any unhedged excess is unwound, and an unwind failure strands ONLY the
unhedged remainder. Never a silent one-sided position.
"""
import asyncio
import dataclasses
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.runner.kalshi_arb as kexec
import bot.core.positions as positions
from bot.runner import BotRunner
from bot.kalshi.cross_arb import (
    KalshiArbOpportunity, _effective_share_cost, _kalshi_taker_fee,
)


def _opp(shares=10, edge=0.05):
    poly_eff = _effective_share_cost(0.50, 0.05)
    return KalshiArbOpportunity(
        event_title="A vs B", poly_event_id="ev1", poly_token="POLY-A",
        poly_team="A", poly_ask_raw=0.50, poly_ask=poly_eff,
        kalshi_ticker="KX-AB-A", kalshi_side="no", kalshi_team="A",
        kalshi_ask=0.45, edge=edge, shares=shares,
        total_cost=shares * (poly_eff + 0.47), guaranteed_profit=shares * 0.05,
    )


def _kalshi_resp(filled, requested=10):
    return {"order": {"status": "executed" if filled else "canceled",
                      "fill_count_fp": f"{filled}.00",
                      "remaining_count_fp": f"{requested - filled}.00"}}


def _poly_resp(cum):
    return {"executions": [{"order": {"cumQuantity": cum}}]}


def _runner(tmp_path, monkeypatch, *, kalshi_buy, poly_cum, kalshi_sell=None, sell_back=0.30):
    monkeypatch.setattr(positions, "_STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(kexec.config, "DRY_RUN", False)
    monkeypatch.setattr(kexec, "send_kalshi_arb_alert", lambda *a, **k: None)
    monkeypatch.setattr(kexec, "fills_log", MagicMock())
    # log_trade writes the real logs/trades.log AND posts its own Discord webhook
    # (gated only by DRY_RUN, which we flip off here) — stub it so tests don't
    # pollute trades.log or fire "A vs B" alerts to the live channel.
    monkeypatch.setattr(kexec, "log_trade", lambda *a, **k: None)
    from bot.poly_us.client import order_filled_qty

    r = BotRunner.__new__(BotRunner)
    r._poly_us = True
    r._order_filled_qty = order_filled_qty
    r.tracker = positions.PositionTracker()
    r._last_miss_alert, r._last_stale_warn, r._last_book_log = {}, {}, {}
    r._last_reject_log = {}
    r._reject_csv = MagicMock()
    r._poly_buying_power, r._kalshi_buying_power = 1000.0, 1000.0
    r._exec_pnl_csv = MagicMock()

    pd = types.SimpleNamespace(last_updated=time.time(), ask_depth=500.0)
    r._poly_feed_adapter = MagicMock()
    r._poly_feed_adapter.get_price.return_value = pd
    r.kalshi_feed = MagicMock()
    r.kalshi_feed.get_age.return_value = 0.5   # fresh (0.0 would hit `or inf`)
    r.kalshi_feed.get_best_bid.return_value = 0.30  # passes exit-liquidity gate
    r.kalshi_feed.fillable_qty.return_value = 10_000  # (diagnostic sampler still uses it)
    r.kalshi_feed.is_suspect.return_value = False     # book trusted (gate passes)
    r._rest_fillable = AsyncMock(return_value=10_000) # ample REST entry depth (gate passes)
    r._log_would_fire = lambda *a, **k: None          # Phase 1.5 logging tested separately

    calls = {"sell": 0, "sell_back": 0, "kbuy_count": None, "poly_qty": None}

    async def create_order(ticker, side, action, count, price, time_in_force="fill_or_kill"):
        if action == "sell":
            calls["sell"] += 1
            return kalshi_sell if kalshi_sell is not None else _kalshi_resp(0, count)
        calls["kbuy_count"] = count        # size of the Kalshi buy (sized from actual fill)
        return kalshi_buy

    async def get_orderbook(ticker, depth=10):
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}

    r.kalshi_client = MagicMock()
    r.kalshi_client.create_order = create_order
    r.kalshi_client.get_orderbook = get_orderbook

    async def place_limit_fok(token, price, qty, label):
        calls["poly_qty"] = qty            # size of the Poly order
        return _poly_resp(poly_cum)

    async def do_sell_back(token, qty, label):
        # sell_back returns (vwap_price, sold_qty) — Poly rewrites FOK→IOC so a sell can
        # partial-fill and the caller must learn HOW MUCH sold. A bare price here would
        # TypeError in _unwind_poly_excess's unpack. Default mock = a full sale.
        calls["sell_back"] += 1
        return (sell_back, float(qty)) if sell_back is not None else (None, 0.0)

    r.client = MagicMock()
    r.client.place_limit_fok = place_limit_fok
    r.client.sell_back = do_sell_back
    # Fire-time Poly re-check: open market + low live ask clears any limit → gate passes.
    r.client.get_fill_quote = AsyncMock(
        return_value=(0.01, "MARKET_STATE_OPEN", [(0.01, 1000.0)], None, {}))
    return r, calls


# ── Implausible-edge (cross-venue divergence) sanity ceiling ─────────────────────────────────
@pytest.mark.asyncio
async def test_implausible_edge_rejected_before_any_order(tmp_path, monkeypatch):
    # A phantom edge above KALSHI_MAX_PLAUSIBLE_EDGE (e.g. the freeze-recovery 70% of wf 44/45) is
    # rejected at the TOP of _execute_kalshi_arb: recorded as implausible_edge, no order placed, so it
    # never reaches fill_success / would_fire / the verdict.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r._log_reject = MagicMock()
    await r._execute_kalshi_arb(_opp(edge=0.70))
    assert r._log_reject.call_args[0][1] == "implausible_edge"      # (opp, reason, detail)
    assert calls["kbuy_count"] is None and calls["poly_qty"] is None   # neither leg fired


@pytest.mark.asyncio
async def test_plausible_large_edge_passes_the_gate(tmp_path, monkeypatch):
    # The largest real edge observed (~0.33) is BELOW the 0.50 ceiling → it passes the implausible_edge
    # gate (proceeds normally; with full fills it hedges). The gate must not reject a real large edge.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r._log_reject = MagicMock()
    await r._execute_kalshi_arb(_opp(edge=0.33))
    reasons = [c[0][1] for c in r._log_reject.call_args_list]
    assert "implausible_edge" not in reasons


@pytest.mark.asyncio
async def test_log_would_fire_sentinels_distinguish_missing_from_zero(monkeypatch):
    # transact_age absent → "" (the book had no transactTime); poly_read_ms absent → "cache_hit"
    # (a ≤1s local-cache hit, no HTTP read, so no MISS latency to measure). Neither is "0": a
    # parser must tell a no-data row from a genuinely 0.0 value. Also confirms wf_id resume (+1).
    monkeypatch.setattr(kexec.asyncio, "create_task", lambda coro: coro.close())
    r = BotRunner.__new__(BotRunner)
    r._poly_us = True
    r._wf_seq = 41
    rows = []
    r._wf_csv = types.SimpleNamespace(writerow=rows.append)
    r._last_edge_alert = {}
    opp = _opp()
    r._log_would_fire(opp, 0.51, 0.47, 200.0, 150.0, None, None)
    assert rows[0][0] == 42                                    # wf_seq resumed (+1)
    # rest_transact_age_s / poly_read_latency_ms are cols 14/15 (liquidity cols 16-25 follow).
    assert rows[0][14] == "" and rows[0][15] == "cache_hit"    # transact_age blank, read=cache_hit
    rows.clear()
    r._log_would_fire(opp, 0.51, 0.47, 200.0, 150.0, 0.0, 0.0)   # a real 0.0 is neither
    assert rows[0][14] == "0.000" and rows[0][15] == "0"


@pytest.mark.asyncio
async def test_rest_fillable_sums_at_limit():
    r = BotRunner.__new__(BotRunner)
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = AsyncMock(return_value={"orderbook_fp": {
        "yes_dollars": [["0.40", "100"], ["0.30", "50"]], "no_dollars": [["0.60", "999"]]}})
    # Buy NO at limit 0.65 → lift yes bids with px >= 1-0.65=0.35 → only the 0.40 level.
    assert await r._rest_fillable("T", "no", 0.65) == 100.0
    # Limit 0.72 → threshold 0.28 → both yes levels qualify → 150.
    assert await r._rest_fillable("T", "no", 0.72) == 150.0


@pytest.mark.asyncio
async def test_rest_fillable_failclosed_on_error():
    r = BotRunner.__new__(BotRunner)
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = AsyncMock(side_effect=RuntimeError("boom"))
    assert await r._rest_fillable("T", "no", 0.5) == 0.0   # error → 0 → skip


@pytest.mark.asyncio
async def test_log_would_fire_writes_row(monkeypatch):
    # Stub the edge alert so the test never posts to a real Discord webhook that
    # might be present in a local .env (this test exercises the real _log_would_fire).
    monkeypatch.setattr(kexec, "send_proper_edge_alert", lambda *a, **k: None)
    r = BotRunner.__new__(BotRunner)
    r._wf_seq = 0
    r._wf_csv = MagicMock()
    async def _noop(*a, **k):
        pass
    r._sample_book_evolution = _noop
    r._log_would_fire(_opp(), poly_limit=0.51, kalshi_limit=0.45, poly_fillable=400.0,
                      kalshi_fillable=500.0)
    assert r._wf_seq == 1
    r._wf_csv.writerow.assert_called_once()
    # First would-fire arms the edge-alert throttle for this (event:ticker:side).
    key = f"{_opp().poly_event_id}:{_opp().kalshi_ticker}:{_opp().kalshi_side}"
    assert key in r._last_edge_alert
    first_ts = r._last_edge_alert[key]
    # A second would-fire within 60s must NOT re-arm (throttled → timestamp unchanged).
    r._log_would_fire(_opp(), poly_limit=0.51, kalshi_limit=0.45, poly_fillable=400.0,
                      kalshi_fillable=500.0)
    assert r._last_edge_alert[key] == first_ts
    await asyncio.sleep(0)  # let the (stubbed) sampler tasks finish


@pytest.mark.asyncio
async def test_log_would_fire_logs_liquidity_columns(monkeypatch):
    # Poly liquidity from poly_stats (fire-path book), Kalshi live from the feed → the 10 trailing
    # phantom-vs-real columns, in header order.
    monkeypatch.setattr(kexec, "send_proper_edge_alert", lambda *a, **k: None)
    r = BotRunner.__new__(BotRunner)
    r._wf_seq = 0
    r._last_edge_alert = {}
    rows = []
    r._wf_csv = types.SimpleNamespace(writerow=rows.append)
    async def _noop(*a, **k):
        pass
    r._sample_book_evolution = _noop
    r.kalshi_feed = MagicMock()
    r.kalshi_feed.get_liquidity.return_value = (68.0, 94.0, 0.54, 1.2)
    r._log_would_fire(
        _opp(), poly_limit=0.51, kalshi_limit=0.45, poly_fillable=400.0, kalshi_fillable=500.0,
        poly_stats={"open_interest": 777.0, "oi_age_s": 5.0, "last_trade_px": 0.85,
                    "last_trade_qty": 9.34, "last_trade_age_s": 3.0, "shares_traded": 1000.0,
                    "notional_traded": 50000.0},
    )
    # liquidity block at fixed cols 16-26 (minutes_since_kickoff follows at 27, blank: no kickoff)
    assert rows[0][16:27] == ["777", "5.0", "0.8500", "9.34", "3.0", "1000", "50000",
                              "68", "94", "0.5400", "1.2"]
    assert rows[0][27] == ""
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_log_would_fire_liquidity_blank_when_absent(monkeypatch):
    # No feed, no poly_stats → 10 trailing blanks (never 0; blank = unavailable).
    monkeypatch.setattr(kexec, "send_proper_edge_alert", lambda *a, **k: None)
    r = BotRunner.__new__(BotRunner)
    r._wf_seq = 0
    r._last_edge_alert = {}
    rows = []
    r._wf_csv = types.SimpleNamespace(writerow=rows.append)
    async def _noop(*a, **k):
        pass
    r._sample_book_evolution = _noop
    r._log_would_fire(_opp(), poly_limit=0.51, kalshi_limit=0.45, poly_fillable=400.0,
                      kalshi_fillable=500.0)
    # The two tick columns trail the liquidity block, so slice them off rather than widen the
    # blank run: kalshi_tick is a real value here and would break a [""]*N assertion.
    assert rows[0][-14:-2] == [""] * 12      # 11 liquidity + minutes_since_kickoff
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_log_would_fire_records_both_ticks(monkeypatch):
    # Both legs' ticks land in the row; an unread poly_tick is BLANK, never the 0.01 majority
    # value — the whole point of the column is catching the market that differs, which it did on
    # its first row (MLB at 0.005, against six files that claimed 0.01).
    monkeypatch.setattr(kexec, "send_proper_edge_alert", lambda *a, **k: None)
    r = BotRunner.__new__(BotRunner)
    r._wf_seq = 0
    r._last_edge_alert = {}
    rows = []
    r._wf_csv = types.SimpleNamespace(writerow=rows.append)
    async def _noop(*a, **k):
        pass
    r._sample_book_evolution = _noop

    opp = _opp()
    opp.kalshi_tick, opp.poly_tick = 0.01, 0.001
    r._log_would_fire(opp, poly_limit=0.51, kalshi_limit=0.45, poly_fillable=400.0,
                      kalshi_fillable=500.0)
    assert rows[0][-2:] == ["0.0010", "0.0100"]

    opp.poly_tick = None                      # unread → blank, NOT "0.0100"
    r._log_would_fire(opp, poly_limit=0.51, kalshi_limit=0.45, poly_fillable=400.0,
                      kalshi_fillable=500.0)
    assert rows[1][-2:] == ["", "0.0100"]
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_minutes_since_kickoff_helper(monkeypatch):
    # now − kickoff in minutes; negative pre-game; None when absent/unparseable.
    monkeypatch.setattr(kexec, "transact_age_s", lambda _k, _now: 630.0)
    assert kexec._minutes_since_kickoff("2026-06-23T17:00:00Z") == pytest.approx(10.5)
    monkeypatch.setattr(kexec, "transact_age_s", lambda _k, _now: -120.0)   # 2 min pre-game
    assert kexec._minutes_since_kickoff("2026-06-23T17:00:00Z") == pytest.approx(-2.0)
    monkeypatch.setattr(kexec, "transact_age_s", lambda _k, _now: None)
    assert kexec._minutes_since_kickoff(None) is None


@pytest.mark.asyncio
async def test_sample_book_evolution_writes_six_samples_with_rest_depth_and_ask(monkeypatch):
    async def _nosleep(_s):
        pass
    monkeypatch.setattr(kexec.asyncio, "sleep", _nosleep)
    r = BotRunner.__new__(BotRunner)
    r._poly_us = True
    pd = types.SimpleNamespace(best_ask=0.50, ask_depth=400.0, best_bid=0.49)
    r._poly_feed_adapter = MagicMock()
    r._poly_feed_adapter.get_price.return_value = pd
    r.client = MagicMock()
    # ONE fetch, read through quote_from_md — the same parser get_fill_quote runs, so the
    # sampler's ask columns cannot drift from what the fire path would see.
    r.client._fetch_book = AsyncMock(return_value={"marketData": {"offers": [
        {"px": {"value": "0.50", "currency": "USD"}, "qty": "300"},
        {"px": {"value": "0.52", "currency": "USD"}, "qty": "100"},
    ]}})
    r.kalshi_feed = MagicMock()
    r.kalshi_feed.get_best_ask.return_value = 0.45
    # Set explicitly: without it _rest_book hits AttributeError and the Kalshi columns come out
    # blank by accident rather than by design, which is not what this test is about.
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = AsyncMock(return_value={"orderbook_fp": {
        "no_dollars": [["0.44", "25"]], "yes_dollars": [["0.55", "500"]]}})
    r._wf_samples_csv = MagicMock()
    await r._sample_book_evolution(1, _opp(), kalshi_limit=0.45)
    assert r._wf_samples_csv.writerow.call_count == 6   # +0/0.25/0.5/1/2/3s
    row = r._wf_samples_csv.writerow.call_args_list[0].args[0]
    # Tail: ..., rest_poly_depth, rest_poly_ask, rest_poly_bid, rest_poly_bid_depth, rest_transact_age
    assert row[-5] == "300"        # REST best-level depth (qty at min ask 0.50)
    assert row[-4] == "0.5000"     # REST ask
    assert row[-1] == ""           # rest_transact_age blank (no transactTime in this mock)


@pytest.mark.asyncio
async def test_sample_book_evolution_records_bid_depths(monkeypatch):
    # The unwind side of both books: what we could SELL, and how much of it. ask−bid alone prices
    # only the top share, so these are what say whether the flatten cost survives at size.
    async def _nosleep(_s):
        pass
    monkeypatch.setattr(kexec.asyncio, "sleep", _nosleep)
    r = BotRunner.__new__(BotRunner)
    r._poly_us = True
    r._poly_feed_adapter = MagicMock()
    r._poly_feed_adapter.get_price.return_value = types.SimpleNamespace(
        best_ask=0.50, ask_depth=400.0, best_bid=0.49)
    # ONE book, read in BOTH directions — the ask ladder via quote_from_md, the bid ladder via
    # _poly_exit_from_book. A long token exits at max(bids), with depth AT that level, not summed.
    r.client = MagicMock()
    r.client._fetch_book = AsyncMock(return_value={"marketData": {
        "offers": [{"px": {"value": "0.50", "currency": "USD"}, "qty": "300"}],
        "bids": [{"px": {"value": "0.49", "currency": "USD"}, "qty": "120"},
                 {"px": {"value": "0.48", "currency": "USD"}, "qty": "900"}]}})
    r.kalshi_feed = MagicMock()
    r.kalshi_feed.get_best_ask.return_value = 0.45
    r.kalshi_feed.get_best_bid.return_value = 0.43
    # _opp() buys the NO side, so the two directions read OPPOSITE arrays:
    #   sell NO  → no_dollars,  best bid = max = 0.44 → depth AT it = 25
    #   buy  NO  → yes_dollars, px >= 1 − limit 0.45  → 500
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = AsyncMock(return_value={"orderbook_fp": {
        "no_dollars": [["0.43", "60"], ["0.44", "25"], ["0.42", "999"]],
        "yes_dollars": [["0.55", "500"]],
    }})
    r._wf_samples_csv = MagicMock()
    await r._sample_book_evolution(1, _opp(), kalshi_limit=0.45)

    row = r._wf_samples_csv.writerow.call_args_list[0].args[0]
    assert row[6] == "0.4300"      # kalshi_bid — the WS value, kept as the cross-transport check
    assert row[7] == "500"         # kalshi_fillable — the BUY side, off the same one snapshot
    # The REST bid and its depth are a STRUCTURAL pair: 0.44 is this book's own best bid, and 25
    # is the depth at THAT price. Note the WS bid (0.43) disagrees by a tick — which is exactly
    # the case that used to silently mis-measure.
    assert row[8] == "0.4400"      # rest_kalshi_bid
    assert row[9] == "25"          # rest_kalshi_bid_depth
    assert row[-5] == "300"        # rest_poly_depth — the ask side, off the SAME one fetch
    assert row[-3] == "0.4900"     # rest_poly_bid
    assert row[-2] == "120"        # rest_poly_bid_depth (best level only)
    # Both Kalshi directions came off ONE book: a sell side doing its own fetch would read 2+.
    # (This test stubs sleep, so all six offsets land inside the 1s cache and collapse to a single
    # read. Real offsets span 3s and take ~4 — the invariant here is one book per sample serving
    # both directions, not one book per sampler.)
    assert r.kalshi_client.get_orderbook.await_count == 1
    # Poly is rate-limited at ~1 req/s and THROTTLES over-limit with a stale 200, so the bid must
    # cost no extra read: one fetch per sample, not two. This is the assertion that keeps it so.
    assert r.client._fetch_book.await_count == 6


async def _kalshi_row(monkeypatch, *, book=None, exc=None):
    """One sample row with the Kalshi book under test; Poly off (us mode disabled)."""
    async def _nosleep(_s):
        pass
    monkeypatch.setattr(kexec.asyncio, "sleep", _nosleep)
    r = BotRunner.__new__(BotRunner)
    r._poly_us = False
    r.feed = MagicMock()
    r.feed.get_price.return_value = types.SimpleNamespace(
        best_ask=0.50, ask_depth=400.0, best_bid=0.49)
    r.kalshi_feed = MagicMock()
    r.kalshi_feed.get_best_ask.return_value = 0.45
    r.kalshi_feed.get_best_bid.return_value = 0.43
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = (AsyncMock(side_effect=exc) if exc
                                     else AsyncMock(return_value=book))
    r._wf_samples_csv = MagicMock()
    await r._sample_book_evolution(1, _opp(), kalshi_limit=0.45)
    return r._wf_samples_csv.writerow.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_sample_book_evolution_blanks_kalshi_depth_when_the_read_raises(monkeypatch):
    # An unreadable book logs BLANK, not 0. _rest_fillable maps it to 0.0 because on the fire path
    # 0 means skip (fail-closed); the sampler has no decision to protect, and a 0 here would read
    # as "book empty" in analysis — a different fact entirely.
    row = await _kalshi_row(monkeypatch, exc=RuntimeError("502"))
    assert row[7] == ""            # kalshi_fillable — unknown, NOT 0
    assert row[8] == ""            # rest_kalshi_bid
    assert row[9] == ""            # rest_kalshi_bid_depth
    assert row[0] == 1             # the row is still written; only the unknown cells are blank


@pytest.mark.asyncio
async def test_sample_book_evolution_blanks_kalshi_depth_on_a_JUNK_200(monkeypatch):
    """The failure that does NOT raise, and so nearly logged a fake measured zero.

    Kalshi 429s rather than throttling, but a 200 carrying an error body or a changed shape still
    arrives as a perfectly good dict. `kbook is not None` is therefore NOT enough to conclude we
    read a book — _rest_book returns None only on a raise. Without the _book_levels_present /
    three-state gates, every one of these rows would log 0: "we looked and the book was empty",
    which is a claim about the market rather than about our read.
    """
    for junk in ({"error": "internal"}, {"orderbook_fp": None},
                 {"orderbook_fp": {"no_dollars": None}}, {"orderbook_fp": "nope"}):
        row = await _kalshi_row(monkeypatch, book=junk)
        assert row[7] == "", f"kalshi_fillable should be blank for {junk!r}"
        assert row[8] == "", f"rest_kalshi_bid should be blank for {junk!r}"
        assert row[9] == "", f"rest_kalshi_bid_depth should be blank for {junk!r}"


@pytest.mark.asyncio
async def test_sample_book_evolution_logs_ZERO_for_a_genuinely_empty_kalshi_book(monkeypatch):
    # The other half of the split: we DID read, and there was nothing there. That is a real
    # observation about the market and must stay distinguishable from the junk-200 rows above.
    row = await _kalshi_row(
        monkeypatch, book={"orderbook_fp": {"no_dollars": [], "yes_dollars": []}})
    assert row[7] == "0"           # kalshi_fillable — read, genuinely empty
    assert row[8] == ""            # rest_kalshi_bid — no bid exists to name
    assert row[9] == "0"           # rest_kalshi_bid_depth — read, nothing to sell into


# ── Sequential execution (poly_first default) ────────────────────────────────

@pytest.mark.asyncio
async def test_poly_first_both_fill_hedged(tmp_path, monkeypatch):
    # Poly fills 10 → Kalshi FOK for exactly 10 fills → hedged, no unwind.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    await r._execute_kalshi_arb(_opp())
    assert r.tracker.has_stranded() is False
    assert len(r.tracker.list_positions()) > 0
    assert calls["sell"] == 0 and calls["sell_back"] == 0
    assert calls["kbuy_count"] == 10               # Kalshi sized to the Poly fill


@pytest.mark.asyncio
async def test_poly_first_kalshi_kill_unwinds_poly(tmp_path, monkeypatch):
    # Poly fills 10, Kalshi FOK kills (0) → flatten the Poly 10 via sell_back → no strand.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(0), poly_cum=10,
                       sell_back=0.30)
    await r._execute_kalshi_arb(_opp())
    assert calls["sell_back"] == 1
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_poly_first_kalshi_kill_sellback_fails_strands(tmp_path, monkeypatch):
    # Poly fills 10, Kalshi kills, Poly sell-back fails → 10 stranded (only residual case).
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(0), poly_cum=10,
                       sell_back=None)
    await r._execute_kalshi_arb(_opp())
    assert r.tracker.has_stranded() is True


@pytest.mark.asyncio
async def test_poly_first_poly_miss_aborts(tmp_path, monkeypatch):
    # Poly misses (0) → nothing else fired: no Kalshi order, no position, no strand.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=0)
    await r._execute_kalshi_arb(_opp())
    assert calls["kbuy_count"] is None             # Kalshi never fired
    assert len(r.tracker.list_positions()) == 0
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_poly_first_partial_poly_sizes_kalshi_to_fill(tmp_path, monkeypatch):
    # Poly partial-fills 6 → Kalshi FOK sized to 6 (NOT opp.shares=10) → hedged 6.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(6, 6), poly_cum=6)
    await r._execute_kalshi_arb(_opp())
    assert calls["kbuy_count"] == 6                # sized from ACTUAL fill, not 10
    assert r.tracker.has_stranded() is False
    assert len(r.tracker.list_positions()) > 0


@pytest.mark.asyncio
async def test_poly_first_partial_kalshi_fok_hedges_fill_unwinds_poly_excess(tmp_path, monkeypatch):
    # Defensive UNDER-fill: Poly fills 10, Kalshi FOK returns a PARTIAL 6 (should be
    # all-or-nothing, but if it misbehaves). Must hedge the 6 it filled and flatten only
    # the 4 Poly excess — NOT flatten all 10 (which would leave 6 Kalshi naked + unrecorded
    # = a silent strand). Symmetric with _exec_kalshi_first's excess handling.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(6, 10), poly_cum=10,
                       sell_back=0.30)
    await r._execute_kalshi_arb(_opp())
    assert calls["sell_back"] == 1                  # only the 4-Poly excess flattened
    assert calls["sell"] == 0                       # no Kalshi unwind (no over-fill)
    assert r.tracker.has_stranded() is False
    positions_held = r.tracker.list_positions()     # add_position records BOTH legs at qty
    assert positions_held and all(p.shares == 6 for p in positions_held)  # hedge = the 6 fill


@pytest.mark.asyncio
async def test_poly_first_kalshi_fok_overfill_unwinds_kalshi_excess(tmp_path, monkeypatch):
    # Defensive OVER-fill mirror: Poly fills 10, Kalshi FOK over-fills 12 (a misbehaving
    # FOK can over-fill as well as under-fill). Must hedge 10 and unwind the 2 excess Kalshi
    # — NOT silently cap the record at 10 and ignore the extra 2 (the mirror silent strand).
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(12, 10), poly_cum=10,
                       kalshi_sell=_kalshi_resp(2, 2))
    await r._execute_kalshi_arb(_opp())
    assert calls["sell"] == 1                        # the 2 excess Kalshi unwound via IOC
    assert calls["sell_back"] == 0                   # no Poly excess to flatten
    assert r.tracker.has_stranded() is False
    positions_held = r.tracker.list_positions()      # add_position records BOTH legs at qty
    assert positions_held and all(p.shares == 10 for p in positions_held)  # hedge capped at Poly fill


def _install_recon_spies(r):
    """Spy the three poly_first reconciliation methods, recording their qty args
    while still calling through (tracker / exec_pnl side effects still happen)."""
    rec = {"hedge": [], "unwind_poly": [], "unwind_kalshi": []}
    orig_hedge, orig_up, orig_uk = r._record_hedge, r._unwind_poly_excess, r._unwind_kalshi_excess

    def _hedge(opp, qty, *a, **k):
        rec["hedge"].append(qty)
        return orig_hedge(opp, qty, *a, **k)

    async def _up(opp, qty, *a, **k):
        rec["unwind_poly"].append(qty)
        return await orig_up(opp, qty, *a, **k)

    async def _uk(opp, qty, *a, **k):
        rec["unwind_kalshi"].append(qty)
        return await orig_uk(opp, qty, *a, **k)

    r._record_hedge, r._unwind_poly_excess, r._unwind_kalshi_excess = _hedge, _up, _uk
    return rec


@pytest.mark.parametrize("kfill,P,exp_hedge,exp_unwind_poly,exp_unwind_kalshi", [
    (10, 10, [10], [], []),   # expected FULL fill → old happy path: record P, no unwind
    (0,  10, [],   [10], []),  # expected KILL → old else-branch: no record, flatten all P
    (6,  10, [6],  [4], []),   # UNDER-fill → hedge the 6, flatten only the 4 Poly excess
    (12, 10, [10], [], [2]),   # OVER-fill → hedge P=10, unwind the 2 Kalshi excess (mirror)
])
@pytest.mark.asyncio
async def test_poly_first_reconciliation_pins_exact_quantities(
    tmp_path, monkeypatch, kfill, P, exp_hedge, exp_unwind_poly, exp_unwind_kalshi,
):
    # Pins the EXACT _record_hedge / _unwind_* arguments for all four FOK fill outcomes —
    # proves the byte-equivalence claim (kfill==P → old happy; kfill==0 → old else) and the
    # two defensive branches (under/over-fill), rather than asserting it in prose.
    excess = max(0, kfill - P)
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(kfill, P), poly_cum=P,
                       sell_back=0.30, kalshi_sell=_kalshi_resp(excess, excess))
    rec = _install_recon_spies(r)
    await r._execute_kalshi_arb(_opp())
    assert rec["hedge"] == exp_hedge
    assert rec["unwind_poly"] == exp_unwind_poly
    assert rec["unwind_kalshi"] == exp_unwind_kalshi
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_poly_first_partial_books_proportional_buckets(tmp_path, monkeypatch):
    # The partial path is the FIRST time one event writes BOTH P&L buckets — assert the
    # split is proportional and separate (the original-sin conflation guard, check 6):
    # marked_unsettled for the 6 hedged, execution_cost for the 4 flattened, no double-count.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(6, 10), poly_cum=10,
                       sell_back=0.30)
    await r._execute_kalshi_arb(_opp())
    rows = [c.args[0] for c in r._exec_pnl_csv.writerow.call_args_list]
    triples = [(row[1], row[2], row[5]) for row in rows]   # (bucket, kind, qty)
    assert ("marked_unsettled", "hedge", 6) in triples      # booked only for the hedged 6
    assert ("execution_cost", "flatten_poly", 4) in triples  # cost only for the flattened 4
    assert len(rows) == 2                                    # exactly two rows — no double-count
    # buckets stay separated: nothing crosses over (no booked-edge on the loss, no loss on the hedge)
    assert not any(b == "marked_unsettled" and q == 4 for b, _, q in triples)
    assert not any(b == "execution_cost" and q == 6 for b, _, q in triples)


@pytest.mark.asyncio
async def test_poly_fire_time_recheck_moved_price_aborts(tmp_path, monkeypatch):
    # Live Poly ask has moved above our limit at fire time (stale-feed phantom) →
    # skip before any order: no Poly fill, no Kalshi, no position, no strand.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.get_fill_quote = AsyncMock(
        return_value=(0.99, "MARKET_STATE_OPEN", [(0.99, 100.0)], None, {}))  # ask > limit
    await r._execute_kalshi_arb(_opp())
    assert calls["poly_qty"] is None                       # Poly never placed
    assert calls["kbuy_count"] is None                     # Kalshi never placed
    assert len(r.tracker.list_positions()) == 0
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_poly_fire_time_recheck_failclosed_on_none(tmp_path, monkeypatch):
    # REST book errored → get_fill_quote (None, '?', []) → fail-closed skip (no orders).
    # Now rejects distinctly as poly_state_unavailable ('?' = no state field) — still no orders.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.get_fill_quote = AsyncMock(return_value=(None, "?", [], None, {}))
    await r._execute_kalshi_arb(_opp())
    assert calls["poly_qty"] is None and calls["kbuy_count"] is None
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_poly_fire_time_recheck_suspended_market_aborts(tmp_path, monkeypatch):
    # Poly market SUSPENDED (post-goal halt): price looks fine but is frozen/untradeable
    # → must skip before any order (the 40% "halt phantom"). No fill, no strand.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.get_fill_quote = AsyncMock(
        return_value=(0.01, "MARKET_STATE_SUSPENDED", [(0.01, 100.0)], None, {}))
    await r._execute_kalshi_arb(_opp())
    assert calls["poly_qty"] is None and calls["kbuy_count"] is None
    assert len(r.tracker.list_positions()) == 0
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_fire_uses_rest_fillable_ignores_frozen_ws_cache(tmp_path, monkeypatch):
    # THE point of A1: poly_fillable at fire comes from the REST book quote (summed over
    # ask levels ≤ limit), NOT the WS cache — which here is a deliberately-frozen phantom.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.get_fill_quote = AsyncMock(
        return_value=(0.40, "MARKET_STATE_OPEN", [(0.40, 3.0)], None, {}))   # REST: 3 fillable @limit
    r._poly_feed_adapter.get_price.return_value = types.SimpleNamespace(
        last_updated=time.time(), ask_depth=726257.0)             # frozen WS phantom
    captured = {}
    r._log_would_fire = lambda opp, pl, kl, pf, kf, *a: captured.update(poly_fillable=pf)
    await r._execute_kalshi_arb(_opp())
    assert captured["poly_fillable"] == 3.0          # REST, NOT the 726257 WS phantom


@pytest.mark.asyncio
async def test_fire_poly_fillable_spans_levels(tmp_path, monkeypatch):
    # fillable-at-limit sums EVERY ask level ≤ poly_limit (~0.5088), not just the best.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.get_fill_quote = AsyncMock(
        return_value=(0.40, "MARKET_STATE_OPEN", [(0.40, 3.0), (0.41, 4.0)], None, {}))
    captured = {}
    r._log_would_fire = lambda opp, pl, kl, pf, kf, *a: captured.update(poly_fillable=pf)
    await r._execute_kalshi_arb(_opp())
    assert captured["poly_fillable"] == 7.0          # 3 + 4, both ≤ limit — not best-level 3


@pytest.mark.asyncio
async def test_fire_none_ask_rejects_poly_not_fillable(tmp_path, monkeypatch):
    # No quote (None ask) while state is OPEN → reject poly_not_fillable, no orders/would_fire.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.get_fill_quote = AsyncMock(return_value=(None, "MARKET_STATE_OPEN", [], None, {}))
    rejects, fired = [], []
    r._log_reject = lambda opp, reason, detail="": rejects.append(reason)
    r._log_would_fire = lambda *a, **k: fired.append(True)
    await r._execute_kalshi_arb(_opp())
    assert "poly_not_fillable" in rejects
    assert fired == [] and calls["poly_qty"] is None and calls["kbuy_count"] is None


@pytest.mark.asyncio
async def test_fire_missing_state_rejects_distinctly(tmp_path, monkeypatch):
    # Book returned NO state field ('?') → DISTINCT poly_state_unavailable reject (not
    # poly_not_fillable), so an empty would_fire from a feed hiccup is greppable. No orders.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.get_fill_quote = AsyncMock(return_value=(0.40, "?", [(0.40, 3.0)], None, {}))
    rejects = []
    r._log_reject = lambda opp, reason, detail="": rejects.append(reason)
    await r._execute_kalshi_arb(_opp())
    assert rejects == ["poly_state_unavailable"]
    assert calls["poly_qty"] is None and calls["kbuy_count"] is None


@pytest.mark.asyncio
async def test_kalshi_first_both_fill_hedged(tmp_path, monkeypatch):
    # kalshi_first: Kalshi FOK fills 10 → Poly sized to 10 fills → hedged.
    monkeypatch.setattr(kexec.config, "KALSHI_EXEC_ORDER", "kalshi_first")
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    await r._execute_kalshi_arb(_opp())
    assert r.tracker.has_stranded() is False
    assert len(r.tracker.list_positions()) > 0
    assert calls["poly_qty"] == 10                 # Poly sized to the Kalshi fill


@pytest.mark.asyncio
async def test_longshot_poly_price_skips_before_firing(tmp_path, monkeypatch):
    # Poly leg below KALSHI_MIN_POLY_PRICE (longshot) → skip entirely: the 95¢ other
    # leg would strand if the cheap leg missed (tonight's $9 loss shape). No orders.
    monkeypatch.setattr(kexec.config, "KALSHI_MIN_POLY_PRICE", 0.10)
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    placed = {"poly": 0}
    _orig = r.client.place_limit_fok
    async def _spy(*a, **k):
        placed["poly"] += 1
        return await _orig(*a, **k)
    r.client.place_limit_fok = _spy

    longshot = dataclasses.replace(_opp(), poly_ask_raw=0.05)
    await r._execute_kalshi_arb(longshot)
    assert placed["poly"] == 0
    assert calls["sell"] == 0 and calls["sell_back"] == 0
    assert len(r.tracker.list_positions()) == 0


@pytest.mark.asyncio
async def test_extreme_kalshi_leg_skips_before_firing(tmp_path, monkeypatch):
    # Kalshi leg near $0 (Poly near $1) → catastrophic void-tail asymmetry → skip.
    # This is the Switzerland 0.97/0.01 trap: real fillable edge, terrible risk/reward.
    monkeypatch.setattr(kexec.config, "KALSHI_MIN_POLY_PRICE", 0.10)
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    extreme = dataclasses.replace(_opp(), poly_ask_raw=0.97, kalshi_ask=0.01)
    await r._execute_kalshi_arb(extreme)
    assert calls["poly_qty"] is None and calls["kbuy_count"] is None
    assert len(r.tracker.list_positions()) == 0
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_high_poly_leg_skips_before_firing(tmp_path, monkeypatch):
    # Poly leg above the band ceiling (1-floor) → complement near $0 → skip.
    monkeypatch.setattr(kexec.config, "KALSHI_MIN_POLY_PRICE", 0.10)
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    await r._execute_kalshi_arb(dataclasses.replace(_opp(), poly_ask_raw=0.95))
    assert calls["poly_qty"] is None and calls["kbuy_count"] is None


@pytest.mark.asyncio
async def test_suspect_book_skips_before_firing(tmp_path, monkeypatch):
    # Book flagged suspect (recent gap/divergence/resnapshot) → skip, no orders.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.kalshi_feed.is_suspect.return_value = True
    placed = {"poly": 0}
    _orig = r.client.place_limit_fok
    async def _spy(*a, **k):
        placed["poly"] += 1
        return await _orig(*a, **k)
    r.client.place_limit_fok = _spy

    await r._execute_kalshi_arb(_opp())
    assert placed["poly"] == 0
    assert len(r.tracker.list_positions()) == 0


@pytest.mark.asyncio
async def test_thin_kalshi_entry_depth_skips_before_firing(tmp_path, monkeypatch):
    # An EMPTY Kalshi book at our limit → skip entirely: no Poly order, no Kalshi order, no
    # position, no strand. Prevents the Poly-fills/Kalshi-misses bleed (buy Poly then unwind at a
    # loss).
    # NOTE: this used to fire on entry_depth=2 vs shares=10 — a PARTIAL book. Under IOC that is no
    # longer a reject: we size down to 2 and fill (see the resize test below). Only a book that
    # cannot fill even ONE share is a real reject now.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r._rest_fillable = AsyncMock(return_value=0)  # REST shows an EMPTY book at our limit
    placed = {"poly": 0}
    _orig = r.client.place_limit_fok
    async def _spy(*a, **k):
        placed["poly"] += 1
        return await _orig(*a, **k)
    r.client.place_limit_fok = _spy

    await r._execute_kalshi_arb(_opp())
    assert placed["poly"] == 0                       # never fired the Poly leg
    assert calls["sell"] == 0 and calls["sell_back"] == 0
    assert len(r.tracker.list_positions()) == 0
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_gather_preserves_gate_order_entry_depth_first(tmp_path, monkeypatch):
    # A2 fetches both REST legs in parallel, but gate ORDER must be unchanged: when BOTH
    # the Kalshi entry-depth gate AND the Poly state gate would fail, the entry-depth
    # reject still wins (evaluated first), exactly as in the sequential version.
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r._rest_fillable = AsyncMock(return_value=0)                       # EMPTY Kalshi (fails)
    r.client.get_fill_quote = AsyncMock(
        return_value=(0.01, "MARKET_STATE_SUSPENDED", [(0.01, 100.0)], None, {}))  # Poly also fails
    rejects = []
    r._log_reject = lambda opp, reason, detail="": rejects.append(reason)
    await r._execute_kalshi_arb(_opp())
    assert rejects == ["thin_entry_depth"]            # entry-depth wins; poly never reached
    assert calls["poly_qty"] is None and calls["kbuy_count"] is None


# ── kalshi_first execution order ──────────────────────────────────────────────────────────────
# _exec_kalshi_first had ONE test mention before this block and has NEVER run: the DRY branch
# returns before dispatch, so both exec helpers are LIVE-ONLY and flipping KALSHI_EXEC_ORDER in
# DRY changes nothing — the first live trade would be that path's first-ever execution.
#
# The data says it should become the default. On 792 economic events (logs/fill_success.csv) the
# KALSHI leg is the one that fails: 64.0% kalshi_moved vs 5.9% poly_moved. poly_first therefore
# fills Poly and then flattens it on 64% of events (2.3 flattens per completed arb — which IS the
# settlement_backtest KILL verdict, Σ flatten 70.60 vs Σ won 4.50), while kalshi_first turns that
# same 64% into a FREE stop at the identical 27.8% both_fill capture rate.
#
# So pin the reconciliation BEFORE any flip. Poly does NOT honor FOK — it coerces to IOC and can
# partial-fill [VERIFIED poly_us/client.py:order_filled_qty] — so P ∈ [0, kfill] is the real
# state space, not all-or-none.


@pytest.fixture
def _kalshi_first(monkeypatch):
    monkeypatch.setattr(kexec.config, "KALSHI_EXEC_ORDER", "kalshi_first")


@pytest.mark.asyncio
async def test_kalshi_first_kill_is_a_free_stop(tmp_path, monkeypatch, _kalshi_first):
    """THE point of kalshi_first: the 66.3% of events where Kalshi won't fill must cost NOTHING —
    Poly is never fired, so there is nothing to flatten. This is the case poly_first pays for."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(0), poly_cum=10)
    await r._execute_kalshi_arb(_opp())
    assert calls["poly_qty"] is None, "Poly must NEVER fire after a Kalshi kill — that's the saving"
    assert calls["sell"] == 0 and calls["sell_back"] == 0   # nothing to unwind
    assert len(r.tracker.list_positions()) == 0
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_kalshi_first_both_fill_hedges_and_sizes_poly_to_actual_kalshi_fill(
        tmp_path, monkeypatch, _kalshi_first):
    """Poly is sized from the ACTUAL Kalshi fill (the mirror of the poly_first invariant),
    never opp.shares."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    await r._execute_kalshi_arb(_opp(shares=10))
    assert calls["kbuy_count"] == 10
    assert calls["poly_qty"] == 10.0        # sized to kfill
    assert calls["sell"] == 0               # fully hedged → no unwind
    # a hedge records BOTH legs (one row per venue)
    assert sorted(p.shares for p in r.tracker.list_positions()) == [10.0, 10.0]
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_kalshi_first_poly_miss_unwinds_the_whole_kalshi_leg(tmp_path, monkeypatch, _kalshi_first):
    """The 5.9% case: Kalshi filled, Poly missed → the FULL Kalshi leg is naked and must be
    unwound. (Note the helper is named _unwind_kalshi_EXCESS, but kalshi_first's dominant
    failure is a FULL-leg unwind — confirm it is genuinely the same path.)"""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=0,
                       kalshi_sell=_kalshi_resp(10))     # unwind sells all 10
    await r._execute_kalshi_arb(_opp(shares=10))
    assert calls["poly_qty"] == 10.0
    assert calls["sell"] == 1                      # the whole leg unwound
    assert len(r.tracker.list_positions()) == 0    # nothing hedged, nothing left
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_kalshi_first_poly_partial_hedges_the_fill_and_unwinds_the_rest(
        tmp_path, monkeypatch, _kalshi_first):
    """Poly coerces FOK→IOC and CAN partial-fill: hedge the 6 that filled, unwind the 4 of Kalshi
    left naked. Hedging 10 would book a phantom hedge; unwinding 10 would dump a good hedge."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=6,
                       kalshi_sell=_kalshi_resp(4))
    await r._execute_kalshi_arb(_opp(shares=10))
    assert calls["sell"] == 1
    # hedge the ACTUAL Poly fill (6), not the 10 requested — both legs recorded at 6
    assert sorted(p.shares for p in r.tracker.list_positions()) == [6.0, 6.0]
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_kalshi_first_strands_only_the_unsold_remainder(tmp_path, monkeypatch, _kalshi_first):
    """kalshi_first's tail risk: the unwind IOC sells into the THINNER book. A partial unwind must
    strand ONLY the remainder (→ loud alert + global pause), never silently vanish."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=0,
                       kalshi_sell=_kalshi_resp(3))     # only 3 of 10 sell → 7 stranded
    await r._execute_kalshi_arb(_opp(shares=10))
    assert r.tracker.has_stranded() is True
    stranded = [p for p in r.tracker.list_positions() if p.is_stranded]
    assert len(stranded) == 1


@pytest.mark.asyncio
async def test_kalshi_first_unwinds_a_poly_overfill(tmp_path, monkeypatch, _kalshi_first):
    """A Poly OVER-fill (P > kfill) must unwind the P − kfill excess.

    _exec_poly_first defends BOTH reconciliation directions and says why: "a misbehaving FOK can
    under-fill OR over-fill, so we defend both directions rather than assume one away."
    _exec_kalshi_first computes only excess_kalshi = kfill − hedged; poly_excess = P − hedged is
    never computed, so the excess Poly is left naked AND UNRECORDED — invisible to the tracker,
    the exposure caps, and the strand alert. Silent, which is worse than a strand.

    Low likelihood (Poly should not over-fill a kfill-sized order) but the same low likelihood
    applies to the kalshi_excess mirror that poly_first defends anyway. _unwind_poly_excess
    already exists — kalshi_first just never calls it."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=13)
    await r._execute_kalshi_arb(_opp(shares=10))
    assert calls["sell_back"] == 1, "the 3 over-filled Poly shares must be flattened, not left naked"


# ── _unwind_poly_excess partial sells (the IOC consequence) ───────────────────────────────────
# Poly rewrites FOK->IOC, so the unwind SELL can partial-fill. Strand only the unsold remainder
# and book the real proceeds — symmetric with _unwind_kalshi_excess.

def _unwind_runner(tmp_path, monkeypatch, *, sell_back_ret):
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    async def do_sell_back(token, qty, label):
        calls["sell_back"] += 1
        calls["sell_back_qty"] = qty
        return sell_back_ret
    r.client.sell_back = do_sell_back
    r._record_exec_cost = MagicMock()
    return r, calls


def _cost_rows(r):
    return [(c[0][0], c[0][2]) for c in r._record_exec_cost.call_args_list]   # (bucket, qty)


@pytest.mark.asyncio
async def test_unwind_poly_partial_strands_only_the_remainder(tmp_path, monkeypatch):
    """Sold 6 of 10 → book flatten_poly for the 6 that really sold and strand ONLY the 4 left.
    Stranding all 10 would report a phantom position and lose the 6's proceeds from P&L."""
    r, calls = _unwind_runner(tmp_path, monkeypatch, sell_back_ret=(0.42, 6.0))
    await r._unwind_poly_excess(_opp(shares=10), 10, "[t]")
    rows = dict(_cost_rows(r))
    assert rows.get("flatten_poly") == 6, "proceeds of the 6 sold must be booked"
    assert rows.get("strand_poly") == 4, "strand ONLY the unsold 4, not all 10"
    assert r.tracker.has_stranded() is True
    stranded = [p for p in r.tracker.list_positions() if p.is_stranded]
    assert stranded[0].shares == 4.0


@pytest.mark.asyncio
async def test_unwind_poly_full_sale_strands_nothing(tmp_path, monkeypatch):
    r, calls = _unwind_runner(tmp_path, monkeypatch, sell_back_ret=(0.42, 10.0))
    await r._unwind_poly_excess(_opp(shares=10), 10, "[t]")
    rows = dict(_cost_rows(r))
    assert rows.get("flatten_poly") == 10
    assert "strand_poly" not in rows
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_unwind_poly_nothing_sold_strands_everything(tmp_path, monkeypatch):
    """No liquidity → (None, 0.0). Strand the full qty and book NO flatten proceeds."""
    r, calls = _unwind_runner(tmp_path, monkeypatch, sell_back_ret=(None, 0.0))
    await r._unwind_poly_excess(_opp(shares=10), 10, "[t]")
    rows = dict(_cost_rows(r))
    assert "flatten_poly" not in rows, "nothing sold → no proceeds to book"
    assert rows.get("strand_poly") == 10
    assert r.tracker.has_stranded() is True


@pytest.mark.asyncio
async def test_poly_first_flattens_when_the_kalshi_FOK_KILLS_VIA_409(tmp_path, monkeypatch):
    """THE go-live blocker. V2 signals a killed FOK by RAISING 409 (V1 returned a zero-fill body).
    _exec_poly_first has no try/except, so the raise escaped AFTER the Poly leg filled and
    _unwind_poly_excess NEVER RAN → a naked, UNRECORDED Poly leg: no hedge, no strand, no global
    pause, no alert. On the DEFAULT exec order, on the MODAL outcome (64.0% kalshi_moved).

    The whole suite missed it because the mock RETURNED a hand-written V1 kill body — the fixture
    was written from the same belief as the code, so it proved the flatten works against a venue
    that no longer exists. This test drives the REAL V2 signal: an exception.
    """
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=None, poly_cum=10)

    async def create_order(ticker, side, action, count, price, time_in_force="fill_or_kill"):
        if action == "sell":
            calls["sell"] += 1
            return _kalshi_resp(0, count)
        calls["kbuy_count"] = count
        # What the real client now returns for a 409 FOK kill: a zero-fill, NOT an exception.
        return {"fill_count": 0, "remaining_count": float(count),
                "status": "canceled", "_fok_killed": True}
    r.kalshi_client.create_order = create_order

    await r._execute_kalshi_arb(_opp(shares=10))

    assert calls["poly_qty"] == 10.0, "Poly fired"
    assert calls["sell_back"] == 1, "the Poly leg MUST be flattened when the Kalshi FOK kills"
    assert len(r.tracker.list_positions()) == 0, "no hedge booked, nothing left naked"
    assert r.tracker.has_stranded() is False, "sell_back succeeded → nothing stranded"


# ── both legs are IOC (price-capped), not FOK ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_both_legs_are_placed_IOC_not_FOK(tmp_path, monkeypatch):
    """Both legs must be price-capped IOC.

    POLY: FOK is not available at all — the venue silently rewrites it to IOC [VERIFIED
    2026-07-15 on a real order: 255/300 partial]. Asking for a guarantee we never get is how a
    false safety claim survived in that docstring, and it leaves us exposed to Poly LATER shipping
    real FOK, which would silently turn our orders all-or-nothing with no code change.

    KALSHI: FOK *is* honored — and that is the problem. On the 87 kalshi_moved events where the
    Kalshi book held partial depth, FOK KILLS → hedge 0, flatten ALL 645 Poly shares, capture 0.
    IOC hedges 218 and flattens only 427 (-34%). Identical on the other 420 (book empty → both
    fill 0). An IOC fills only at prices <= our limit, and the limit IS the breakeven ask, so
    every filled share is profitable: same edge floor, more of it captured.
    """
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    tifs = {}

    async def create_order(ticker, side, action, count, price, time_in_force="fill_or_kill"):
        tifs[action] = time_in_force
        if action == "sell":
            calls["sell"] += 1
            return _kalshi_resp(0, count)
        calls["kbuy_count"] = count
        return _kalshi_resp(10)
    r.kalshi_client.create_order = create_order

    async def place_limit_fok(token, price, qty, label, **kw):
        calls["poly_qty"] = qty
        return _poly_resp(10)
    r.client.place_limit_fok = place_limit_fok

    await r._execute_kalshi_arb(_opp(shares=10))
    assert tifs.get("buy") == "immediate_or_cancel", \
        "the Kalshi entry leg must be IOC — FOK throws away partial fills"


@pytest.mark.asyncio
async def test_kalshi_partial_fill_hedges_it_instead_of_flattening_everything(tmp_path, monkeypatch):
    """The whole point of the swap. Poly filled 10; the Kalshi book only had 4.
    FOK would kill → flatten all 10 Poly, capture 0. IOC fills 4 → hedge 4, flatten 6."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(4), poly_cum=10)
    await r._execute_kalshi_arb(_opp(shares=10))
    assert sorted(p.shares for p in r.tracker.list_positions()) == [4.0, 4.0], \
        "hedge the 4 that filled — not 0 (FOK) and not 10 (phantom)"
    assert calls["sell_back"] == 1        # the 6 unhedged Poly are flattened
    assert r.tracker.has_stranded() is False


# ── THE HEDGE INVARIANT: a partial on one leg ⇒ the SAME amount on the other ──────────────────
# Both legs are now IOC, so EITHER can partial-fill. The rule: the second leg is sized to the
# FIRST leg's ACTUAL fill, and whatever can't be matched is unwound — so we always end MATCHED,
# never with a half-hedge. Under FOK a partial kfill was impossible, so kalshi_first was never
# tested for it; IOC makes it reachable and these close that gap.

def _legs(r):
    """(poly_shares, kalshi_shares) currently recorded."""
    pos = {p.token_id: p.shares for p in r.tracker.list_positions()}
    return pos.get("POLY-A", 0.0), pos.get("KX-AB-A", 0.0)


@pytest.mark.asyncio
async def test_poly_first_half_poly_fill_buys_half_the_kalshi(tmp_path, monkeypatch):
    """Poly fills 5 of 10 → the Kalshi order must be for 5, and we end MATCHED 5/5."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(5, 5), poly_cum=5)
    await r._execute_kalshi_arb(_opp(shares=10))
    assert calls["kbuy_count"] == 5, "Kalshi must be sized to the ACTUAL Poly fill (5), not 10"
    assert _legs(r) == (5.0, 5.0), "matched — no half-hedge"
    assert calls["sell_back"] == 0 and r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_poly_first_half_kalshi_fill_flattens_the_poly_remainder(tmp_path, monkeypatch):
    """Poly filled 10 but Kalshi only fills 4 (IOC partial — impossible under FOK).
    Hedge 4, flatten the 6 Poly we cannot hedge. End MATCHED 4/4, nothing naked."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(4, 10), poly_cum=10)
    await r._execute_kalshi_arb(_opp(shares=10))
    assert _legs(r) == (4.0, 4.0), "hedge only what BOTH legs filled"
    assert calls["sell_back"] == 1, "the 6 unhedgeable Poly shares must be flattened"
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_kalshi_first_half_kalshi_fill_buys_half_the_poly(tmp_path, monkeypatch):
    """THE NEW CASE the IOC swap creates. Kalshi fills 4 of 10 → the Poly order must be for 4
    (never opp.shares=10 — that would buy 6 Poly with nothing to hedge them). End MATCHED 4/4."""
    monkeypatch.setattr(kexec.config, "KALSHI_EXEC_ORDER", "kalshi_first")
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(4, 10), poly_cum=4)
    await r._execute_kalshi_arb(_opp(shares=10))
    assert calls["poly_qty"] == 4.0, "Poly must be sized to the ACTUAL Kalshi fill (4), not 10"
    assert _legs(r) == (4.0, 4.0), "matched"
    assert calls["sell"] == 0 and r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_kalshi_first_both_partial_unwinds_the_kalshi_remainder(tmp_path, monkeypatch):
    """Kalshi fills 4, then Poly only fills 2 → hedge 2, unwind the 2 Kalshi left naked.
    End MATCHED 2/2 — never 4 Kalshi against 2 Poly."""
    monkeypatch.setattr(kexec.config, "KALSHI_EXEC_ORDER", "kalshi_first")
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(4, 10), poly_cum=2,
                       kalshi_sell=_kalshi_resp(2, 2))
    await r._execute_kalshi_arb(_opp(shares=10))
    assert _legs(r) == (2.0, 2.0), "matched at the SMALLER of the two fills"
    assert calls["sell"] == 1, "the 2 unhedged Kalshi must be unwound"
    assert r.tracker.has_stranded() is False


# ── ambiguous Kalshi failure after the Poly leg filled ────────────────────────────────────────
@pytest.mark.asyncio
async def test_poly_first_kalshi_raise_strands_and_pauses_instead_of_guessing(tmp_path, monkeypatch):
    """Poly filled; the Kalshi order RAISED (timeout/502/reset). We do NOT know whether Kalshi
    filled, and BOTH guesses can strand the other leg:
      assume kfill=0 -> flatten Poly -> if it DID fill we are naked KALSHI, plus a flatten cost
      assume filled  -> do nothing  -> if it did NOT we are naked POLY
    So don't guess: strand what we KNOW we hold (P Poly) -> loud alert + global pause. The
    reconciler then asks both venues what we actually hold and surfaces any Kalshi leg within
    ~2 polls.

    The 409 FOK-kill is NOT this case — that is an unambiguous zero-fill and still flattens
    normally (test_poly_first_flattens_when_the_kalshi_FOK_KILLS_VIA_409).
    """
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)

    async def create_order(ticker, side, action, count, price, time_in_force="fill_or_kill"):
        if action == "sell":
            calls["sell"] += 1
            return _kalshi_resp(0, count)
        calls["kbuy_count"] = count
        raise TimeoutError("kalshi POST timed out")          # ambiguous: filled? unknown
    r.kalshi_client.create_order = create_order
    r._record_exec_cost = MagicMock()

    await r._execute_kalshi_arb(_opp(shares=10))             # must NOT raise out

    assert calls["sell_back"] == 0, "must NOT flatten — that guesses kfill=0 and can strand Kalshi"
    assert r.tracker.has_stranded() is True, "unknown state must PAUSE trading"
    stranded = [p for p in r.tracker.list_positions() if p.is_stranded]
    assert len(stranded) == 1 and stranded[0].shares == 10.0, "strand the Poly we KNOW we hold"
    buckets = [c[0][0] for c in r._record_exec_cost.call_args_list]
    assert "strand_poly" in buckets, "the loss cap must see it"


@pytest.mark.asyncio
async def test_partial_kalshi_depth_RESIZES_instead_of_rejecting(tmp_path, monkeypatch):
    """S2. The Kalshi book holds 4 against our target of 10. Under FOK that was a reject
    (thin_entry_depth) — correct then, because 0 depth meant 0 fill and a stranded Poly leg. Both
    legs are IOC now, so we fill what's there: size to 4 and hedge 4/4.

    This is the 17% of kalshi_moved events (median depth 3 vs a median-7 target) that the FOK-era
    gate threw away — the exact population the FOK→IOC swap exists to capture. The gate and the
    swap's rationale contradicted each other in the same file.
    """
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(4, 4), poly_cum=4)
    r._rest_fillable = AsyncMock(return_value=4)          # the Kalshi book holds 4
    rejects = []
    r._log_reject = lambda opp, reason, detail="": rejects.append(reason)

    await r._execute_kalshi_arb(_opp(shares=10))

    assert "thin_entry_depth" not in rejects, "partial depth must RESIZE, not reject"
    assert calls["poly_qty"] == 4.0, "Poly sized to the Kalshi book, not the 10 target"
    assert calls["kbuy_count"] == 4
    assert _legs(r) == (4.0, 4.0), "matched 4/4"
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_resize_carries_cost_and_profit_with_shares(tmp_path, monkeypatch):
    """The resize must carry total_cost and guaranteed_profit WITH shares. _record_hedge does
    `guaranteed_profit * (qty/opp.shares)`, so moving shares alone books the 10-share profit on a
    4-share fill.

    Asserts the INVARIANTS, not fixture numbers: this file's _opp() fabricates
    guaranteed_profit=shares*0.05 independently of total_cost, so it does NOT satisfy
    `profit = shares - cost` (the relation _size_kalshi_opportunity actually produces). The resize
    recomputes from that relation, which is right — so pin the relation.
    """
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(4, 4), poly_cum=4)
    r._rest_fillable = AsyncMock(return_value=4)
    opp = _opp(shares=10)
    cost_per_share = opp.total_cost / opp.shares
    await r._execute_kalshi_arb(opp)
    assert opp.shares == 4                                          # resized in place
    assert opp.total_cost / opp.shares == pytest.approx(cost_per_share), "per-share cost unchanged"
    assert opp.guaranteed_profit == pytest.approx(opp.shares * 1.0 - opp.total_cost), \
        "profit must satisfy shares - cost after the resize"


@pytest.mark.asyncio
async def test_hedge_books_the_ACTUAL_fill_cost_not_the_detection_price(tmp_path, monkeypatch):
    """S3. Both legs are IOC and can sweep to a breakeven-wide limit, so the realized cost can be
    materially worse than detection. trades.log's `cost` feeds settlement_scorer -> realized_settled
    -> the sizing authority the loss cap acts on, so booking detection prices overstates it.

    Both venues report the real cost exactly (Kalshi average_fill_price + average_fee_paid; Poly
    avgPx + commissionNotionalTotalCollected) — no fee model needed.
    """
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=None, poly_cum=10)
    trades = []
    monkeypatch.setattr(kexec, "log_trade", lambda rec: trades.append(rec))

    async def create_order(ticker, side, action, count, price, time_in_force="fill_or_kill"):
        calls["kbuy_count"] = count
        # Real V2 create shape: filled 10 at a VWAP of 0.5000 with a $0.10 order fee -> 0.51/contract
        # A REAL fee: 10 @ 0.50 -> ceil(0.07*10*0.5*0.5) to the centicent = 0.1750.
        # 0.1000 (the old fixture) matches neither basis, so the fee-basis discriminator in
        # kalshi_avg_fill_cost now refuses it — correctly. See test_actual_fill_price.
        return {"fill_count": "10.00", "remaining_count": "0.00",
                "average_fill_price": "0.5000", "average_fee_paid": "0.1750"}
    r.kalshi_client.create_order = create_order

    async def place_limit_fok(token, price, qty, label):
        calls["poly_qty"] = qty
        # Real Poly shape: 10 @ avgPx 0.4000 with $0.10 commission -> 0.41/share
        return {"executions": [{"order": {
            "cumQuantity": 10, "avgPx": {"value": "0.4000", "currency": "USD"},
            "commissionNotionalTotalCollected": {"value": "0.1000", "currency": "USD"}}}]}
    r.client.place_limit_fok = place_limit_fok

    await r._execute_kalshi_arb(_opp(shares=10))

    assert len(trades) == 1
    t = trades[0]
    assert t["poly_ask_eff"] == pytest.approx(0.41), "Poly booked at the ACTUAL avgPx+commission"
    assert t["kalshi_ask"] == pytest.approx(0.5175), "Kalshi booked at the ACTUAL VWAP+fee"
    assert t["cost"] == pytest.approx(10 * (0.41 + 0.5175)), "cost = actual, not detection"
    assert t["guaranteed_profit"] == pytest.approx(10 - 10 * (0.41 + 0.5175))
    # detection-time kept alongside, so slippage is measurable
    assert t["poly_ask_eff_detect"] == pytest.approx(_opp().poly_ask)
    assert t["cost"] != pytest.approx(_opp(10).total_cost), "must differ from the detection cost"


@pytest.mark.asyncio
async def test_an_EMPTY_kalshi_book_still_lands_in_fill_success(tmp_path, monkeypatch):
    """fill_success must record EVERY edge that reaches the REST re-read, including the ones a gate
    then rejects — it is the dataset both_fill/kalshi_moved are computed from.

    The empty-Kalshi-book case (entry_depth=0) is historically 420/507 of kalshi_moved: its single
    biggest failure class. A `return` placed before this logging silently drops it, which INFLATES
    both_fill by shrinking the denominator — self-flattering, in the number the go-live verdict
    rests on. That regression shipped on 2026-07-15 (S2 moved the log after the gate) and produced
    zero fill_success rows across a live slate.
    """
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r._rest_fillable = AsyncMock(return_value=0)      # EMPTY Kalshi book → thin_entry_depth reject
    logged, rejects = [], []
    r._log_fill_success = lambda *a, **k: logged.append(a)
    r._log_reject = lambda opp, reason, detail="": rejects.append(reason)

    await r._execute_kalshi_arb(_opp(shares=10))

    assert rejects == ["thin_entry_depth"], "the empty book must still reject — it cannot fill"
    assert len(logged) == 1, "...and must STILL be recorded in fill_success (the reject is the data)"
    assert logged[0][3] == 0, "entry_depth=0 is the value that makes it a kalshi_moved row"
    assert calls["poly_qty"] is None, "nothing fired"


# ── _rest_book / _rest_fillable split ────────────────────────────────────────
# The sampler needs the BOOK (to read the sell side), not just the fillable number, so the cached
# fetch was split out. These pin the split: the fire path's fail direction must be unchanged, and
# the two must not become two reads.

@pytest.mark.asyncio
async def test_rest_fillable_still_fails_closed_to_zero_on_an_unreadable_book():
    """0.0, NOT None or a raise. On the fire path 0 means "no depth" → skip, which is the safe
    direction; _rest_book returning None (unknown) must not leak out to a caller that would
    treat it as truthy or crash on it."""
    r = BotRunner.__new__(BotRunner)
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = AsyncMock(side_effect=RuntimeError("502 bad gateway"))
    assert await r._rest_fillable("KX-AB-A", "no", 0.45) == 0.0


@pytest.mark.asyncio
async def test_rest_book_returns_none_on_failure_and_does_not_cache_it():
    # None = UNKNOWN, distinct from an empty book. A failed read must not be cached as though it
    # were an answer — the next caller has to be free to retry.
    r = BotRunner.__new__(BotRunner)
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = AsyncMock(side_effect=RuntimeError("502"))
    assert await r._rest_book("KX-AB-A") is None
    assert await r._rest_book("KX-AB-A") is None
    assert r.kalshi_client.get_orderbook.await_count == 2


@pytest.mark.asyncio
async def test_rest_book_serves_both_directions_from_one_cached_read():
    # The whole reason for the split: the gate and the sampler read the SAME snapshot, so the
    # sell side costs no request AND describes the same book the buy side saw.
    book = {"orderbook_fp": {"no_dollars": [["0.43", "60"]], "yes_dollars": [["0.55", "500"]]}}
    r = BotRunner.__new__(BotRunner)
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = AsyncMock(return_value=book)
    assert await r._rest_fillable("KX-AB-A", "no", 0.45) == 500.0
    assert kexec._sell_fillable_from_book(
        await r._rest_book("KX-AB-A"), "no", 0.43) == 60.0
    assert r.kalshi_client.get_orderbook.await_count == 1


async def _poly_row(monkeypatch, book):
    """One sample row with the Poly book under test (us mode on, Kalshi stubbed readable)."""
    async def _nosleep(_s):
        pass
    monkeypatch.setattr(kexec.asyncio, "sleep", _nosleep)
    r = BotRunner.__new__(BotRunner)
    r._poly_us = True
    r._poly_feed_adapter = MagicMock()
    r._poly_feed_adapter.get_price.return_value = types.SimpleNamespace(
        best_ask=0.50, ask_depth=400.0, best_bid=0.49)
    r.client = MagicMock()
    r.client._fetch_book = AsyncMock(return_value=book)
    r.kalshi_feed = MagicMock()
    r.kalshi_feed.get_best_ask.return_value = 0.45
    r.kalshi_feed.get_best_bid.return_value = 0.43
    r.kalshi_client = MagicMock()
    r.kalshi_client.get_orderbook = AsyncMock(return_value={"orderbook_fp": {
        "no_dollars": [["0.44", "25"]], "yes_dollars": [["0.55", "500"]]}})
    r._wf_samples_csv = MagicMock()
    await r._sample_book_evolution(1, _opp(), kalshi_limit=0.45)
    return r._wf_samples_csv.writerow.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_sample_book_evolution_blanks_poly_bid_depth_on_a_JUNK_200(monkeypatch):
    """The Poly mirror of the junk-200 case — _poly_exit_from_book has the same collapse.

    It returns (None, 0.0) for "no marketData", "bids key null" AND "read, no bids" alike, so an
    unguarded call would log rest_poly_bid_depth=0 for a body we never parsed. Poly throttles with
    a stale-but-valid 200 rather than an error, which makes this rarer than the Kalshi case — and
    correspondingly easier to trust wrongly if it ever fires.
    """
    for junk in ({"error": "internal"}, {}, {"marketData": {"bids": None, "offers": None}}):
        row = await _poly_row(monkeypatch, junk)
        assert row[10] == "", f"rest_poly_depth should be blank for {junk!r}"
        assert row[11] == "", f"rest_poly_ask should be blank for {junk!r}"
        assert row[12] == "", f"rest_poly_bid should be blank for {junk!r}"
        assert row[13] == "", f"rest_poly_bid_depth should be blank for {junk!r}"


@pytest.mark.asyncio
async def test_sample_book_evolution_logs_ZERO_for_a_genuinely_empty_poly_book(monkeypatch):
    """Read it, nothing there — a real observation, and distinct from the junk rows above.

    rest_poly_depth logged BLANK here until 2026-07-15, which dropped the row from
    settlement_backtest._poly_depth_suspect's REST series entirely — so the clearest phantom of
    all (a WS depth of 726257 against a book that is genuinely empty) fell back to the weaker
    price heuristic instead of being caught by the cross-transport check.
    """
    row = await _poly_row(monkeypatch, {"marketData": {"offers": [], "bids": []}})
    assert row[10] == "0"          # rest_poly_depth — read, genuinely no asks
    assert row[11] == ""           # rest_poly_ask — no price exists to name
    assert row[12] == ""           # rest_poly_bid — ditto
    assert row[13] == "0"          # rest_poly_bid_depth — read, genuinely no bids


@pytest.mark.asyncio
async def test_sample_book_evolution_poly_depth_zero_reaches_the_freeze_detector(monkeypatch):
    """The reason the line above matters, stated as the consumer sees it.

    _poly_depth_suspect drops blanks from its REST series (_series → _num → None). A phantom WS
    depth against an empty real book is the single most clear-cut case it exists to catch, and it
    only reaches the cross-transport check if the empty book logs a number.
    """
    from scripts.settlement_backtest import _depth_disagrees
    row = await _poly_row(monkeypatch, {"marketData": {"offers": [], "bids": []}})
    ws_phantom, rest_real = 726257.0, float(row[10])      # the real 2026-06-20 phantom, vs this row
    assert _depth_disagrees(ws_phantom, rest_real) is True
    # ...and a thin book is still not a phantom: 3-vs-0 sits under the absolute floor.
    assert _depth_disagrees(3.0, rest_real) is False


# ── an ambiguous Poly order failure ───────────────────────────────────────────────────────────
# place_limit_fok used to swallow timeout/502/reset and return None, which became P=0 and read as
# a clean miss — "MISSED (no fill)", under a comment saying "no exposure". But a real IOC kill is
# a 200 with cumQuantity 0; only the ambiguous cases raise, and synchronousExecution blocks the
# order ~61ms server-side so a timeout lands INSIDE the fill window. Poly fires FIRST by default.

@pytest.mark.asyncio
async def test_poly_first_ambiguous_poly_error_does_not_read_as_a_clean_miss(tmp_path, monkeypatch):
    """Leg 1 errored: we may hold up to opp.shares and cannot tell. Nothing else fired, so there is
    nothing to unwind and no second leg at risk — do NOT guess a qty (that books a position we may
    not hold, and a phantom cost into the loss cap). Be loud; the reconciler resolves it.
    Symmetric with _exec_kalshi_first's handling of ITS first leg erroring."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.place_limit_fok = AsyncMock(side_effect=RuntimeError("read timeout"))
    crit = []
    monkeypatch.setattr(kexec.log, "critical", lambda m, *a, **k: crit.append(m))

    await r._execute_kalshi_arb(_opp())

    assert any("UNKNOWN" in m for m in crit), "an unknown leg state must be CRITICAL, not a quiet miss"
    assert calls["kbuy_count"] is None, "must not fire Kalshi against a Poly fill we cannot confirm"
    assert r.tracker.list_positions() == [], "must not record a position we may not hold"
    assert r.tracker.has_stranded() is False, "nothing to strand — the reconciler resolves this"


@pytest.mark.asyncio
async def test_poly_first_a_REAL_kill_is_still_a_quiet_miss(tmp_path, monkeypatch):
    """The other side of the split: the venue ANSWERED and filled nothing. Nothing fired, there
    genuinely is no exposure, and this must NOT be escalated to CRITICAL — else the loud signal
    stops meaning anything."""
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=0)
    crit = []
    monkeypatch.setattr(kexec.log, "critical", lambda m, *a, **k: crit.append(m))

    await r._execute_kalshi_arb(_opp())

    assert crit == [], "a genuine kill is an outcome, not an alarm"
    assert calls["kbuy_count"] is None            # None = never fired (harness convention)
    assert r.tracker.has_stranded() is False


@pytest.mark.asyncio
async def test_kalshi_first_ambiguous_poly_error_strands_the_KNOWN_kalshi_leg(tmp_path, monkeypatch):
    """The mirror, and here we DO hold something. Kalshi filled 10 (confirmed) and the Poly leg is
    unknown. Strand the fact we have — which pauses globally — and let the reconciler resolve Poly.
    Guessing either way strands the other leg."""
    monkeypatch.setattr(kexec.config, "KALSHI_EXEC_ORDER", "kalshi_first")
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(10), poly_cum=10)
    r.client.place_limit_fok = AsyncMock(side_effect=RuntimeError("502 bad gateway"))
    crit = []
    monkeypatch.setattr(kexec.log, "critical", lambda m, *a, **k: crit.append(m))

    await r._execute_kalshi_arb(_opp())

    assert any("UNKNOWN" in m for m in crit)
    assert r.tracker.has_stranded() is True, "a confirmed Kalshi leg + unknown Poly must PAUSE"
    stranded = [p for p in r.tracker.list_positions() if p.is_stranded]
    assert [(p.token_id, p.shares) for p in stranded] == [("KX-AB-A", 10.0)], \
        "strand the leg we KNOW we hold — the Kalshi one — at its actual fill"


# ── S3's other half: the unwinds book the ACTUAL fill, not detection ──────────────────────────

@pytest.mark.asyncio
async def test_poly_flatten_books_the_ACTUAL_entry_not_the_raw_detection_ask(tmp_path, monkeypatch):
    """S3 fixed _record_hedge and stopped there. flatten_poly kept booking
    `(opp.poly_ask_raw - price) * sold` — the RAW detection ask, so the loss was short by the Poly
    fee before slippage even counted. safety.is_exec_cost_cap_hit reads exactly that file.

    Here: Poly fills 10 at a real avgPx of 0.4600 with $0.10 commission -> 0.47/share actual, vs a
    0.50 detection ask. Kalshi kills, so all 10 flatten at a 0.30 sell_back.
    """
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(0), poly_cum=10,
                       sell_back=0.30)
    r.client.place_limit_fok = AsyncMock(return_value={"executions": [{"order": {
        "cumQuantity": 10, "avgPx": {"value": "0.4600", "currency": "USD"},
        "commissionNotionalTotalCollected": {"value": "0.1000", "currency": "USD"}}}]})
    booked = []
    r._record_exec_cost = lambda kind, opp, qty, amount, detail: booked.append((kind, qty, amount))

    await r._execute_kalshi_arb(_opp())

    flat = [b for b in booked if b[0] == "flatten_poly"]
    assert len(flat) == 1
    _, qty, amount = flat[0]
    assert qty == 10
    # ACTUAL entry 0.47 (0.46 avgPx + 0.10/10 commission), sold at 0.30 -> 0.17/share.
    assert amount == pytest.approx(10 * (0.47 - 0.30)), \
        "must book the venue's own fill report, not opp.poly_ask_raw"


@pytest.mark.asyncio
async def test_kalshi_unwind_books_the_ACTUAL_sell_not_the_limit_it_sent(tmp_path, monkeypatch):
    """flatten_kalshi booked `(opp.kalshi_ask - sell_price) * sold` and discarded sell_resp
    entirely — detection entry against the LIMIT we asked for. The IOC can sweep below that limit
    and pays a fee on the way out, so both halves flattered the loss.

    Here: Kalshi over-fills (12 vs a Poly 10), so 2 unwind. The sale reports a real fill at 0.3500
    against a limit of ~0.28.
    """
    monkeypatch.setattr(kexec.config, "KALSHI_EXEC_ORDER", "kalshi_first")
    fee_pc = _kalshi_taker_fee(0.3500, 2)
    sell_resp = {"fill_count": "2.00", "remaining_count": "0.00",
                 "average_fill_price": "0.3500", "average_fee_paid": f"{fee_pc * 2:.6f}"}
    buy_resp = {"fill_count": "12.00", "remaining_count": "0.00",
                "average_fill_price": "0.4600", "average_fee_paid": f"{_kalshi_taker_fee(0.46, 12) * 12:.6f}"}
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=buy_resp, poly_cum=10,
                       kalshi_sell=sell_resp)
    booked = []
    r._record_exec_cost = lambda kind, opp, qty, amount, detail: booked.append((kind, qty, amount))

    await r._execute_kalshi_arb(_opp())

    flat = [b for b in booked if b[0] == "flatten_kalshi"]
    assert len(flat) == 1, f"expected one flatten_kalshi, got {booked}"
    _, qty, amount = flat[0]
    assert qty == 2
    entry = 0.46 + _kalshi_taker_fee(0.46, 12)     # actual buy: price + fee
    exit_px = 0.35 - fee_pc                        # actual sale: price MINUS fee (a sell nets it)
    assert amount == pytest.approx(2 * (entry - exit_px)), \
        "must book the actual buy AND the actual sale, not the detection ask and the sell limit"


@pytest.mark.asyncio
async def test_a_bookkeeping_error_does_not_strand_a_position_that_SOLD(tmp_path, monkeypatch):
    """The try used to span the sale AND the bookkeeping, so a raise after a clean sale stranded
    the FULL qty and booked a second cost on top of the flatten already recorded — a double-counted
    loss and a strand record larger than the real remainder, from an error unrelated to the order.

    This is not hypothetical: a missing import raised exactly here while writing this fix, turning
    a clean 10/10 sale into "unwind FAILED, 10 stranded". Disk-full is the realistic trigger —
    these CSVs are never rotated and _record_exec_cost's writerow is unguarded.
    """
    monkeypatch.setattr(kexec.config, "KALSHI_EXEC_ORDER", "kalshi_first")
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=_kalshi_resp(12), poly_cum=10,
                       kalshi_sell=_kalshi_resp(2, requested=2))

    def _boom(*a, **k):
        raise OSError("No space left on device")
    r._record_exec_cost = _boom

    with pytest.raises(OSError):
        await r._execute_kalshi_arb(_opp())

    # The sale happened. It must NOT be recorded as a strand.
    stranded = [p for p in r.tracker.list_positions() if p.is_stranded]
    assert stranded == [], "a bookkeeping failure must not strand contracts that actually sold"


@pytest.mark.asyncio
async def test_record_hedge_fallback_is_the_EFFECTIVE_detection_cost_not_the_raw_ask(
        tmp_path, monkeypatch):
    """S3's fallback said "fall back to the detection price". It fell back to the RAW one.

        opp.poly_ask   -> EFFECTIVE (fee already in it)
        opp.kalshi_ask -> RAW       (fee is NOT)

    so `k_eff = opp.kalshi_ask` was fee-FREE — ~1.7c/share short against a 2c minimum edge, ~85%
    of it, in the flattering direction, on the number settlement_scorer turns into realized_settled.
    The docstring already warns the fallback is optimistic; it was optimistic by more than it said.

    Here the Kalshi fill report is unreadable (no average_fee_paid), so the fallback fires.
    """
    unreadable = {"fill_count": "10.00", "remaining_count": "0.00",
                  "average_fill_price": "0.4500"}          # no fee -> kalshi_avg_fill_cost -> None
    r, calls = _runner(tmp_path, monkeypatch, kalshi_buy=unreadable, poly_cum=10)
    trades = []
    monkeypatch.setattr(kexec, "log_trade", lambda rec: trades.append(rec))
    # Poly's report is unreadable too, so both legs take the fallback.
    r.client.place_limit_fok = AsyncMock(return_value={"executions": [{"order": {
        "cumQuantity": 10}}]})

    opp = _opp()
    await r._execute_kalshi_arb(opp)

    assert len(trades) == 1
    t = trades[0]
    expected_k = opp.kalshi_ask + _kalshi_taker_fee(opp.kalshi_ask)
    assert t["kalshi_ask"] == pytest.approx(expected_k), \
        "the Kalshi fallback must include the fee — opp.kalshi_ask is the RAW ask"
    assert t["kalshi_ask"] > opp.kalshi_ask, "a fee-free fallback under-costs the leg"
    assert t["poly_ask_eff"] == pytest.approx(opp.poly_ask), \
        "the Poly fallback is already effective — unchanged"
