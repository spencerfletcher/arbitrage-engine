"""Tests for fat-spike diagnostic logging — the >5% gate, the Kalshi REST ask-derivation, the
throttle/error status flagging, and the output-path hard-bar. Pure + mocked; no live endpoints."""
import pytest
from types import SimpleNamespace

from bot.runner import kalshi_arb
from bot.runner.kalshi_arb import (
    KalshiArbMixin, _FatSpikeWS, _best_ask_from_book, _FAT_SPIKE_EDGE_THRESHOLD,
)
from bot.runner.runner import _assert_fat_spike_path


class _FakeWriter:
    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(row)


# ── 1. >5% gate triggers capture, sub-5% does not, dedup once per lifecycle ───────────────────

def _should_capture(edge: float, arb_id: int, captured: set) -> bool:
    """The exact gate predicate from the lifecycle-loop hook (kalshi_arb.py)."""
    return edge > _FAT_SPIKE_EDGE_THRESHOLD and arb_id not in captured


def test_fat_spike_gate_threshold_and_dedup():
    captured: set[int] = set()
    assert _should_capture(0.06, 1, captured) is True      # >5% fires
    assert _should_capture(0.04, 2, captured) is False     # <5% does not
    assert _should_capture(0.05, 3, captured) is False     # exactly 5% is NOT >5%
    captured.add(1)                                        # first capture recorded
    assert _should_capture(0.06, 1, captured) is False     # same lifecycle → deduped


# ── 2. Kalshi REST ask = 1 − best-opposite-bid, same side-convention as the WS feed ───────────

def test_best_ask_from_book_derivation():
    # yes_dollars = yes BIDS (best 0.55); no_dollars = no BIDS (best 0.40).
    book = {"orderbook_fp": {
        "yes_dollars": [["0.50", "10"], ["0.55", "20"], ["0.30", "5"]],
        "no_dollars": [["0.40", "7"], ["0.35", "3"]],
    }}
    # Buy YES → lift NO bids → yes_ask = 1 - max(no bids 0.40) = 0.60.
    assert _best_ask_from_book(book, "yes") == 0.60
    # Buy NO  → lift YES bids → no_ask = 1 - max(yes bids 0.55) = 0.45.
    assert _best_ask_from_book(book, "no") == 0.45


def test_best_ask_from_book_matches_feed_side_convention():
    """A flipped side here would invert every Kalshi REST ask and make WS-vs-REST 'divergence' a
    code artifact. Pin the mapping to bot/kalshi/feed.py:get_best_ask
    (yes_ask = 1 - max(no bids); no_ask = 1 - max(yes bids))."""
    book = {"orderbook_fp": {
        "yes_dollars": [["0.61", "1"], ["0.58", "1"]],
        "no_dollars": [["0.22", "1"], ["0.31", "1"]],
    }}
    yes_bids = [0.61, 0.58]
    no_bids = [0.22, 0.31]
    assert _best_ask_from_book(book, "yes") == round(1.0 - max(no_bids), 6)
    assert _best_ask_from_book(book, "no") == round(1.0 - max(yes_bids), 6)


def test_best_ask_from_book_empty_and_garbage():
    assert _best_ask_from_book({"orderbook_fp": {"no_dollars": []}}, "yes") is None   # no opposite bids
    assert _best_ask_from_book({}, "no") is None
    # garbage levels skipped, valid one still used: buy NO → max(yes bids 0.70) → 0.30.
    book = {"orderbook_fp": {"yes_dollars": [["x", "y"], ["0.70", "9"]]}}
    assert _best_ask_from_book(book, "no") == round(1.0 - 0.70, 6)


# ── 3. Throttled/slow + error are FLAGGED, never silently logged as real readings ─────────────

class _Stub(KalshiArbMixin):
    """Minimal carrier for _capture_fat_spike — only the attributes it touches."""
    def __init__(self, poly_quote, get_orderbook):
        self._fat_spike_csv = _FakeWriter()
        self.client = SimpleNamespace(get_fill_quote=poly_quote)
        self.kalshi_client = SimpleNamespace(get_orderbook=get_orderbook)


def _opp():
    return SimpleNamespace(
        kalshi_side="no", event_title="A vs B", poly_token="tok",
        kalshi_ticker="KX-1", edge=0.06,
    )


def _ws():
    return _FatSpikeWS(poly_ask=0.50, poly_depth=300.0, kalshi_ask=0.45, kalshi_depth=None)


_GOOD_BOOK = {"orderbook_fp": {"yes_dollars": [["0.55", "100"]], "no_dollars": [["0.40", "50"]]}}


async def _good_poly(_token, *, fresh=False):
    return (0.50, "MARKET_STATE_OPEN", [(0.50, 200.0)], None, {})


async def _good_orderbook(_ticker, depth=20):
    return _GOOD_BOOK


# column indices (see runner.py header)
K_WS_DEPTH = 17
P_STATUS, P_REST_ASK, P_REST_DEPTH = 12, 13, 14
K_STATUS, K_REST_ASK, K_REST_DEPTH, K_DEPTH_WS = 20, 21, 22, 23
P_TRANSACT_AGE, K_WS_AGE = 24, 25     # appended last so the indices above stay fixed
# liquidity columns (26-36), appended after the book-age cols
P_OI, P_OI_AGE, P_LAST_PX, P_LAST_QTY, P_LAST_AGE, P_SHARES, P_NOTIONAL = 26, 27, 28, 29, 30, 31, 32
K_OI, K_VOL, K_LAST_PX, K_LIQ_AGE = 33, 34, 35, 36
M_KICKOFF = 37                        # minutes_since_kickoff, appended last


@pytest.fixture
def _single_sample(monkeypatch):
    monkeypatch.setattr(kalshi_arb, "_FAT_SPIKE_BURST_N", 1)
    monkeypatch.setattr(kalshi_arb, "_FAT_SPIKE_INTERVAL_S", 0.0)


async def test_capture_logs_ok_with_rest_and_ws_values(_single_sample, monkeypatch):
    monkeypatch.setattr(kalshi_arb, "_FAT_SPIKE_SLOW_MS", 1e9)   # nothing is slow
    s = _Stub(_good_poly, _good_orderbook)
    await s._capture_fat_spike(7, _opp(), _ws(), 0.0)
    assert len(s._fat_spike_csv.rows) == 1
    row = s._fat_spike_csv.rows[0]
    assert row[P_STATUS] == "ok" and row[K_STATUS] == "ok"
    assert row[P_REST_ASK] == "0.5000" and row[P_REST_DEPTH] == "200"
    # buy NO → rest_ask = 1 - max(yes bids 0.55) = 0.45; depth fillable at 0.45 = 100.
    assert row[K_REST_ASK] == "0.4500" and row[K_REST_DEPTH] == "100"
    # WS detection values are logged from the frozen snapshot, not re-read.
    assert row[8] == "0.5000" and row[16] == "0.4500"          # poly_ws_ask, kalshi_ws_ask
    # ghost column = REST depth at the WS ask (0.45) = 100 here (WS == REST this case).
    assert row[K_DEPTH_WS] == "100"
    # ticker-mode WS depth (None in the frozen snapshot) → explicit sentinel, never a blank gap.
    assert row[K_WS_DEPTH] == "ticker_mode"
    # Book-age columns blank here: this poly quote has no transactTime, and the stub has no feed.
    assert row[P_TRANSACT_AGE] == "" and row[K_WS_AGE] == ""
    # Liquidity + minutes_since_kickoff blank: empty stats, no kalshi_feed, opp has no kickoff.
    assert row[26:] == [""] * 12
    assert len(row) == 38


async def test_capture_populates_book_ages_when_available(_single_sample, monkeypatch):
    """poly_transact_age_s from the REST book's transactTime, kalshi_ws_age_s from the feed —
    so a sampled book carries real freshness, not just an offset-from-detection."""
    monkeypatch.setattr(kalshi_arb, "_FAT_SPIKE_SLOW_MS", 1e9)
    monkeypatch.setattr(kalshi_arb, "transact_age_s", lambda _t, _now: 0.142)

    async def _poly_with_transact(_token, *, fresh=False):
        return (0.50, "MARKET_STATE_OPEN", [(0.50, 200.0)], "2026-06-23T04:00:00.000000000Z", {})

    s = _Stub(_poly_with_transact, _good_orderbook)
    s.kalshi_feed = SimpleNamespace(get_age=lambda _t: 3.5)   # WS ask 3.5s old
    await s._capture_fat_spike(7, _opp(), _ws(), 0.0)
    row = s._fat_spike_csv.rows[0]
    assert row[P_TRANSACT_AGE] == "0.142"
    assert row[K_WS_AGE] == "3.5"


async def test_capture_populates_liquidity_columns(_single_sample, monkeypatch):
    """Poly liquidity from the book stats, Kalshi live from the feed → the 10 trailing columns."""
    monkeypatch.setattr(kalshi_arb, "_FAT_SPIKE_SLOW_MS", 1e9)

    async def _poly_with_stats(_token, *, fresh=False):
        return (0.50, "MARKET_STATE_OPEN", [(0.50, 200.0)], None,
                {"open_interest": 507004.69, "oi_age_s": 5.0, "last_trade_px": 0.85,
                 "last_trade_qty": 9.34, "last_trade_age_s": 3.0, "shares_traded": 530166.54,
                 "notional_traded": 44274039.23})

    s = _Stub(_poly_with_stats, _good_orderbook)
    s.kalshi_feed = SimpleNamespace(get_age=lambda _t: 3.5,
                                    get_liquidity=lambda _t: (68.0, 94.0, 0.54, 1.2))
    await s._capture_fat_spike(7, _opp(), _ws(), 0.0)
    row = s._fat_spike_csv.rows[0]
    assert row[P_OI] == "507005" and row[P_OI_AGE] == "5.0"
    assert row[P_LAST_PX] == "0.8500" and row[P_LAST_QTY] == "9.34" and row[P_LAST_AGE] == "3.0"
    assert row[P_SHARES] == "530167" and row[P_NOTIONAL] == "44274039"
    assert row[K_OI] == "68" and row[K_VOL] == "94"
    assert row[K_LAST_PX] == "0.5400" and row[K_LIQ_AGE] == "1.2"


async def test_capture_flags_slow_when_latency_exceeds_threshold(_single_sample, monkeypatch):
    monkeypatch.setattr(kalshi_arb, "_FAT_SPIKE_SLOW_MS", -1.0)  # any latency counts as slow
    s = _Stub(_good_poly, _good_orderbook)
    await s._capture_fat_spike(7, _opp(), _ws(), 0.0)
    row = s._fat_spike_csv.rows[0]
    assert row[P_STATUS] == "slow" and row[K_STATUS] == "slow"  # flagged, not silently logged
    assert row[P_REST_ASK] == "0.5000"                          # value still captured alongside flag


async def test_capture_flags_error_and_blanks_rest(_single_sample, monkeypatch):
    monkeypatch.setattr(kalshi_arb, "_FAT_SPIKE_SLOW_MS", 1e9)

    async def _boom_poly(_token, *, fresh=False):
        raise RuntimeError("poly down")

    async def _boom_orderbook(_ticker, depth=20):
        raise RuntimeError("kalshi down")

    s = _Stub(_boom_poly, _boom_orderbook)
    await s._capture_fat_spike(7, _opp(), _ws(), 0.0)
    row = s._fat_spike_csv.rows[0]
    assert row[P_STATUS] == "error" and row[K_STATUS] == "error"
    assert row[P_REST_ASK] == "" and row[K_REST_ASK] == ""      # corrupted sample → blank, never faked


# ── 4. The new log can NEVER be written as would_fire / execution_pnl ──────────────────────────

@pytest.mark.parametrize("bad", [
    "logs/would_fire.csv",
    "./logs/would_fire.csv",
    "logs/would_fire_samples.csv",
    "logs/execution_pnl.csv",
    "logs/../logs/would_fire.csv",
    "execution_pnl.csv",
])
def test_fat_spike_cannot_write_backtest_or_losscap_file(bad):
    with pytest.raises(ValueError):
        _assert_fat_spike_path(bad)


def test_fat_spike_allows_its_own_output():
    _assert_fat_spike_path("logs/fat_spike_samples.csv")        # no raise
