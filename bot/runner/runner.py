"""BotRunner — orchestrates every async task and holds all shared state.

The per-concern method bodies live in mixins (poly_arb, kalshi_arb, ladder,
macro, balance); this module owns __init__ (all state), the stranded-leg control
loop (global pause + resume), and start() (the asyncio.gather of all loops)."""
from __future__ import annotations

import asyncio
import csv
import os
import time

from bot.core import config
from bot.core.logger import get_logger
from bot.core.types import MarketPair
from bot.core.positions import PositionTracker
from bot.core.alerts import send_strand_pause_alert
from bot.core.safety import is_paused, is_daily_loss_cap_hit, is_exec_cost_cap_hit
from bot.core.ws_timing import ws_timer, run_loop_lag_probe
from bot.polymarket.client import PolymarketClient
from bot.polymarket.feed import OrderBookCache
from bot.poly_us.scanner import PolyUSScanner
from bot.poly_us.feed import PolyUSOrderBookCache
from bot.kalshi.client import KalshiClient
from bot.kalshi.feed import KalshiOrderBookCache
from bot.kalshi.scanner import KalshiScanner
from bot.kalshi.matcher import CrossPlatformPair
from bot.kalshi.macro_pairs import MacroPair
from bot.runner.common import _PolyUSFeedAdapter
from bot.runner.poly_arb import PolyArbMixin
from bot.runner.kalshi_arb import KalshiArbMixin, _SIZE_CURVE_POINTS
from bot.runner.settlement_scorer import SettlementScorer
from bot.runner.macro import MacroMixin
from bot.runner.balance import BalanceMixin
from bot.runner.reconcile import ReconcileMixin

log = get_logger(__name__)


def _max_id_col0(*paths: str) -> int:
    """Highest integer id in column 0 across one or more CSVs (0 if all missing/empty).
    Lets a per-run counter resume monotonically across restarts so ids stay unique — used
    for positive_edges.csv's arb_id (EDGE #N) and would_fire.csv's wf_id. For wf_id we scan
    BOTH would_fire.csv AND would_fire_samples.csv so the backtest join can't collide even if
    one is rotated/truncated independently of the other. Missing/unreadable paths are skipped,
    never fatal (a counter that can't read its floor must not wedge startup)."""
    mx = 0
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="") as f:
                r = csv.reader(f)
                next(r, None)  # header
                for row in r:
                    if row:
                        try:
                            mx = max(mx, int(row[0]))
                        except (ValueError, IndexError):
                            continue
        except OSError:
            continue
    return mx


def _assert_fat_spike_path(path: str) -> None:
    """Refuse to open the fat-spike diagnostic log as a backtest/loss-cap file. A hard assertion
    mirroring scripts/settlement_capture.assert_safe_output: fat_spike_samples.csv records
    hypothetical WS-vs-REST diagnostics and must NEVER be written as would_fire(_samples).csv
    (corrupts the settlement-backtest join) or execution_pnl.csv (would feed a fake loss into the
    live loss cap). Basename match catches the reserved names in ANY directory."""
    forbidden = {"would_fire.csv", "would_fire_samples.csv", "execution_pnl.csv"}
    if os.path.basename(os.path.normpath(path)) in forbidden:
        raise ValueError(
            f"fat_spike refuses to write '{path}': that is a reserved backtest/loss-cap file. "
            f"Use logs/fat_spike_samples.csv."
        )


def _open_versioned_csv(path: str, header: list[str], tag: str):
    """Open `path` for append, guaranteeing its first row matches `header`. If an existing file's
    header differs (a column was added/renamed), rotate the old file to
    logs/rotated/<stem>.pre-<tag>.csv and start a fresh file with the new header. Returns
    (file, writer).

    This automates the manual rotation the repo already does by hand for schema changes
    (logs/rotated/would_fire.pre-transacttime.csv, rejected_edges.pre-freshness.csv, …): a bot
    restart after a column add would otherwise append wider rows under the stale on-disk header,
    producing ragged rows that break the name-based DictReader joins the backtest/analysis rely on.
    Triggering only on an actual header mismatch means an unchanged schema is never rotated."""
    existing: list[str] | None = None
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, newline="") as f:
                existing = next(csv.reader(f), None)
        except OSError:
            existing = None
    if existing is not None and existing != header:
        os.makedirs("logs/rotated", exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        os.replace(path, f"logs/rotated/{stem}.pre-{tag}.csv")
        existing = None
    fh = open(path, "a", newline="", buffering=1)
    w = csv.writer(fh)
    if existing is None:
        w.writerow(header)
    return fh, w


class BotRunner(PolyArbMixin, KalshiArbMixin, MacroMixin,
                BalanceMixin, ReconcileMixin):
    def __init__(self):
        if config.POLY_VENUE == "us":
            from bot.poly_us.client import PolyUSClient, order_is_filled, order_filled_qty
            self.client = PolyUSClient()
            self._order_is_filled = order_is_filled
            self._order_filled_qty = order_filled_qty
            self._poly_us = True
            self._poly_us_scanner = PolyUSScanner(self.client._sdk)
            self._poly_us_feed = PolyUSOrderBookCache(self.client._sdk)
            self._poly_feed_adapter = _PolyUSFeedAdapter(self._poly_us_feed)
            # Vestigial global-Polymarket feed (v1 venue plumbing). Unused on the live us path —
            # kept only so the venue-selection code has a value to fall back to. See ARCHITECTURE.md.
            self.feed = OrderBookCache()
            log.info("✅ Polymarket US client initialized")
        else:
            self.client = PolymarketClient()
            self._poly_us = False
            self._poly_us_scanner = None
            self._poly_us_feed = None
            self._poly_feed_adapter = None
            self.feed = OrderBookCache()
        self.tracker = PositionTracker()
        # Cached buying power per venue (refreshed by _peak_balance_loop, decremented
        # after fills). Used to cap arb sizing so a leg can't exceed available funds
        # and strand the other. 0.0 = unknown → no cap (MAX_POSITION_USD governs).
        self._poly_buying_power: float = 0.0
        self._kalshi_buying_power: float = 0.0
        # Throttle "missed" alerts per event so a re-firing match can't spam.
        self._last_miss_alert: dict[str, float] = {}
        # Throttle stale-price skip warnings (dead feed re-fires every tick).
        self._last_stale_warn: dict[str, float] = {}
        # Throttle the diagnostic Kalshi orderbook snapshot per ticker.
        self._last_book_log: dict[str, float] = {}
        # Kill-switch / daily-loss-cap gate state. The loss-cap check reads
        # trades.log, so _trading_halted() caches the verdict and re-evaluates at
        # most every _HALT_CHECK_INTERVAL — the execution loop fires up to 4×/s.
        self._last_halt_check: float = 0.0
        self._halted: bool = False
        self.active_pairs: list[MarketPair] = []
        self._norm_table: dict[str, str] = {}
        self.scan_num = 0
        self.last_hunt_time = 0.0
        self._last_kalshi_trigger: float = 0.0
        # Schema-drift detector state (see KalshiArbMixin._check_schema_drift): per-sport
        # consecutive "0 matched cross-pairs while Kalshi has live markets" cycle counts +
        # the series already alerted this episode (re-armed on recovery). Catches a silent
        # venue schema rename — the 2026-06-23 ~5.5h matching blackout had no alert.
        self._schema_drift_cycles: dict[str, int] = {}
        self._schema_drift_alerted: set[str] = set()
        # Persistence-confirm tracker: key (event:ticker:side) → (first-seen ts, last opp)
        # above the trade threshold. An edge must hold KALSHI_CONFIRM_SECONDS before firing;
        # the opp is kept so a vanished-before-confirming edge can be logged as rejected.
        self._confirm_seen: dict[str, tuple] = {}
        self._execution_lock = asyncio.Lock()
        # Kickoff-window double-confirm: tracks "I saw this (event, side)
        # in the previous poll." Key = f"{event_id}:{poly_team}". Value = ts.
        # Entries are pruned to at most ~2 poll intervals old.
        self._kickoff_seen: dict[str, float] = {}
        # Alert de-dup: {f"{event_id}:{bookmaker_name}": (last_ts, last_edge)}
        # Prevents re-alerting the same arb on every poll when it persists.
        self._alert_sent: dict[str, tuple[float, float]] = {}
        self.kalshi_client = KalshiClient()
        self.kalshi_feed = KalshiOrderBookCache(
            self.kalshi_client,
            # LADDER SERIES REMOVED 2026-07-15 with the ladder path. Subscribing KXBTCD/
            # KXBTCMAXY put 24/7 BTC ticks on this socket, and the frozen-book watchdog reads a
            # single socket-wide _last_book_change_ts — so BTC reset the 240s timer forever and
            # the watchdog could never bite even if every SPORTS book was frozen.
            config.KALSHI_SERIES + config.KALSHI_MACRO_SERIES,
        )
        self.kalshi_scanner = KalshiScanner(self.kalshi_client)
        self._kalshi_pairs: list[CrossPlatformPair] = []
        self._macro_pairs: list[MacroPair] = []

        os.makedirs("logs", exist_ok=True)
        # Sample interval for active profitable edges (seconds).
        # Active edge tracker: key -> (first_seen_ts, last_seen_ts, peak_edge,
        # label, last_logged_edge_str). Used to detect start/end, measure
        # duration, and suppress duplicate SAMPLE rows at an unchanged edge.
        self._active_edges: dict[str, tuple[float, float, float, str, str, int]] = {}
        # Fat-spike (>5% edge) diagnostic dedup: arb_ids already captured this lifecycle, so a
        # persistent fat edge bursts once (not every tick). Discarded in the lifecycle END branch.
        self._fat_spike_captured: set[int] = set()
        # Detected-edge (every positive crossing) lifecycle CSV: one row per START/SAMPLE/END.
        _pedge_path = "logs/positive_edges.csv"
        # Monotonic counter: one unique id per edge lifecycle (START→END), so interleaved
        # arbs on the same teams stay distinguishable. Seed from the max arb_id already on
        # disk so EDGE #N stays unique + continuous across restarts (was resetting to 0).
        # poly_age_s/kalshi_age_s = each leg's quote staleness at the START/SAMPLE instant, so the
        # edge lifecycle is dateable per leg (blank on END — the legs aren't re-read there). Scan the
        # rotated pre-leg-age file too so arb_id stays monotonic across the schema rotation.
        self._edge_seq: int = _max_id_col0(_pedge_path, "logs/rotated/positive_edges.pre-leg-age.csv")
        self._pedge_csv_file, self._pedge_csv = _open_versioned_csv(
            _pedge_path,
            [
                "arb_id", "timestamp", "event", "game",
                "poly_team", "poly_ask", "poly_depth",
                "kalshi_team", "kalshi_side", "kalshi_ticker", "kalshi_ask",
                "edge", "peak_edge", "duration_s", "shares", "est_profit",
                "poly_age_s", "kalshi_age_s",
            ],
            "leg-age",
        )

        # Near-miss edge CSV: only log when edge is actually profitable (edge >= 0).
        # Written at every tightest-miss sample so you can analyze the full edge distribution
        # and opportunity persistence offline. Columns defined by the header row below.
        _csv_path = "logs/edge_freshness.csv"
        _csv_is_new = not os.path.exists(_csv_path) or os.path.getsize(_csv_path) == 0
        self._edge_csv_file = open(_csv_path, "a", newline="", buffering=1)
        self._edge_csv = csv.writer(self._edge_csv_file)
        if _csv_is_new:
            self._edge_csv.writerow([
                "timestamp", "event_title", "poly_team", "poly_token",
                "poly_ask_raw", "poly_ask_eff", "poly_age_s", "poly_depth",
                "kalshi_ticker", "kalshi_side", "kalshi_team",
                "kalshi_ask", "kalshi_age_s", "kalshi_depth", "edge", "combined_cost",
            ])

        # Rejected-edge CSV: one row per confirmed edge killed at an EXECUTION gate
        # (band / suspect / stale / no-exit / thin-depth / poly-not-fillable). Lets you
        # audit offline whether the gates are discarding viable edges, and which gate.
        # Throttled per (event:ticker:side:reason) so a persistent reject can't flood.
        _rej_path = "logs/rejected_edges.csv"
        _rej_new = not os.path.exists(_rej_path) or os.path.getsize(_rej_path) == 0
        self._reject_csv_file = open(_rej_path, "a", newline="", buffering=1)
        self._reject_csv = csv.writer(self._reject_csv_file)
        if _rej_new:
            self._reject_csv.writerow([
                "timestamp", "event", "poly_team", "poly_token",
                "poly_ask", "poly_age_s", "poly_depth",
                "kalshi_ticker", "kalshi_side", "kalshi_ask", "kalshi_age_s", "kalshi_depth",
                "edge", "shares", "reason", "detail",
            ])
        self._last_reject_log: dict[str, float] = {}

        # Phase 1.5 would-fire log: one row per opp that passes EVERY gate (dry or
        # live), plus a post-detection book-evolution sample. Feeds the settlement
        # backtest — the free, pre-capital test of whether edges are real.
        # Seed the would-fire id from the max already on disk so it stays UNIQUE and
        # monotonic across restarts. A naive 0-reset collided wf_ids after each
        # OOM/restart, corrupting the would_fire ↔ would_fire_samples join the
        # backtest relies on. Scan BOTH files so a reset can't happen even if would_fire.csv
        # is rotated/truncated independently of its samples sidecar.
        _wf_path = "logs/would_fire.csv"
        _wfs_path = "logs/would_fire_samples.csv"
        # The transactTime/cache-bust schema change rotates the prior (CDN-contaminated) files
        # to logs/rotated/*.pre-transacttime.csv. Scan those too so wf_id stays monotonic across
        # the cutover — a fresh file must not reset the counter and collide with rotated rows.
        self._wf_seq = _max_id_col0(
            _wf_path, _wfs_path,
            "logs/rotated/would_fire.pre-transacttime.csv",
            "logs/rotated/would_fire_samples.pre-transacttime.csv",
            "logs/rotated/would_fire.pre-liquidity.csv",
            "logs/rotated/would_fire_samples.pre-kalshibid.csv",
            "logs/rotated/would_fire.pre-ticks.csv",
            "logs/rotated/would_fire_samples.pre-biddepth.csv",
        )
        # Liquidity/activity columns appended (phantom-vs-real context): Poly from the fire-path
        # book stats (fresh), Kalshi live from the ticker WS. _open_versioned_csv auto-rotates the
        # pre-liquidity file on the header change (scanned above for wf_id continuity).
        #
        # Both venues' price ticks appended 2026-07-15. They belong HERE and not in the samples
        # sidecar because a tick is static per market — sampling it six times per would-fire would
        # log the same number six times. Recorded because the tick is PER-MARKET and varies WITHIN
        # a series, which silently changed both the Poly buffer and what poly_fillable counts — it
        # caught that on its FIRST row (wf 265: MLB at 0.005, not the 0.01 every doc claimed), and
        # that finding is what got the buffer dropped (S4 → COMPLETED.md). It still decides which
        # rungs poly_fillable sums. Values live in the venue-reference skill.
        # poly_tick is blank on the dormant global path, which has no per-market tick to read.
        self._wf_csv_file, self._wf_csv = _open_versioned_csv(
            _wf_path,
            [
                "wf_id", "timestamp", "event", "poly_token", "kalshi_ticker",
                "kalshi_side", "poly_ask_raw", "kalshi_ask", "edge", "shares",
                "poly_limit", "kalshi_limit", "poly_fillable", "kalshi_fillable",
                "rest_transact_age_s", "poly_read_latency_ms",
                "poly_open_interest", "poly_oi_age_s", "poly_last_trade_px", "poly_last_trade_qty",
                "poly_last_trade_age_s", "poly_shares_traded", "poly_notional_traded",
                "kalshi_open_interest", "kalshi_volume", "kalshi_last_trade_px", "kalshi_liq_age_s",
                "minutes_since_kickoff", "poly_tick", "kalshi_tick",
            ],
            "ticks",
        )
        # kalshi_bid added 2026-07-15 — the price a Kalshi unwind sells into, the mirror of
        # poly_bid. It was missing because the sampler was built for poly_first, which flattens
        # POLY: the instrument only ever watched the leg the incumbent design unwinds, so the
        # alternative (kalshi_first) could not be costed at all. The Poly flatten cost has n=251
        # from this file; the Kalshi unwind had n=1, from the exec-order probe alone.
        #
        # NOTE: this file previously used a raw append with a first-run header, unlike its
        # would_fire.csv parent — so a column add would have appended wider rows under the stale
        # on-disk header and produced the ragged rows _open_versioned_csv exists to prevent.
        # Migrated to the versioned opener; the pre-kalshibid file rotates and is scanned above for
        # wf_id continuity.
        #
        # Bid DEPTHS added 2026-07-15, both venues. The bid columns above price a flatten at
        # top-of-book, which silently assumes the whole size clears at the best bid — true at the
        # 8-share ramp, and the assumption the ~500-share question turns on. These say where it
        # stops being true. Both are REST, because they are a SIZING input and the WS depth that
        # would have been free here is the freeze-prone source that once logged 726257 against a
        # real book of 3. Neither costs a request: each side is read off the same snapshot its
        # ask/fillable column already fetched, so the bid and ask also describe ONE instant.
        #
        # Each REST bid is logged WITH the price it was measured at (rest_kalshi_bid /
        # rest_poly_bid), both derived structurally from that same book — so a depth always
        # belongs to the price beside it. The WS kalshi_bid/poly_bid stay as the cross-transport
        # check, which is all they can honestly be: pricing a REST depth off a WS bid would read
        # two transports against one threshold and silently log 0 when they disagree by a tick.
        self._wf_samples_file, self._wf_samples_csv = _open_versioned_csv(
            _wfs_path,
            [
                "wf_id", "offset_s", "poly_ask", "poly_depth", "poly_bid",
                "kalshi_ask", "kalshi_bid", "kalshi_fillable",
                "rest_kalshi_bid", "rest_kalshi_bid_depth",
                "rest_poly_depth", "rest_poly_ask", "rest_poly_bid", "rest_poly_bid_depth",
                "rest_transact_age",
            ],
            "biddepth",
        )

        # VWAP size-vs-edge curve (see kalshi_arb._size_curve). Measures the one number that decides
        # SCALABILITY and is otherwise unmeasurable: the logged `edge` is TOP-OF-BOOK and decays to
        # ~0 at the breakeven limit, so `edge x fillable` is circular — this walks BOTH ladders and
        # records the post-fee edge actually realizable at each size, plus best_n = argmax(n*edge).
        # Context: size is a CHOSEN testing ramp, not a depth limit (only 3% of both_fills are
        # depth-capped; 78% have >=1000 hedgeable vs a median target of 8) — CLAUDE.md Posture.
        # Pure observability: computed from levels already fetched, never feeds a gate, never raises.
        _sc_path = "logs/size_curve.csv"
        _sc_new = not os.path.exists(_sc_path) or os.path.getsize(_sc_path) == 0
        self._size_curve_file = open(_sc_path, "a", newline="", buffering=1)
        self._size_curve_csv = csv.writer(self._size_curve_file)
        if _sc_new:
            self._size_curve_csv.writerow([
                "timestamp", "wf_id", "event", "kalshi_ticker", "kalshi_side",
                "edge_top", "shares_target", "poly_fillable", "kalshi_fillable",
                *[f"edge_at_{n}" for n in _SIZE_CURVE_POINTS],
                "best_n", "best_profit",
            ])

        # §5 inter-leg-window probe (DRY-only; see kalshi_arb._probe_interleg_window). Measures the
        # one number that gates the poly_first-vs-kalshi_first decision and is otherwise unmeasurable
        # in DRY (fills.log only fills on live execution): how often would the Kalshi leg drop below
        # `shares` in the poly-fill→kalshi-fire window. Pure observability — never feeds a gate.
        _ilp_path = "logs/interleg_probe.csv"
        _ilp_new = not os.path.exists(_ilp_path) or os.path.getsize(_ilp_path) == 0
        self._interleg_probe_file = open(_ilp_path, "a", newline="", buffering=1)
        self._interleg_probe_csv = csv.writer(self._interleg_probe_file)
        if _ilp_new:
            self._interleg_probe_csv.writerow([
                "timestamp", "event", "kalshi_ticker", "kalshi_side", "shares", "edge",
                "poly_fillable", "entry_depth_t0", "entry_depth_t1", "delay_ms", "kalshi_would_kill",
            ])

        # Exec-order probe (DRY-only, observability — never feeds a gate). Two questions §5 can't
        # answer: (a) what each venue's UNWIND would actually cost, modelled exactly as the real
        # unwind paths price it — because the exec-order decision is P(kalshi kills) x poly_flatten
        # vs P(poly kills) x kalshi_unwind (64.0% vs 5.9% on economic events), not either cost
        # alone; (b) the kalshi_first MIRROR of §5 — after the 17ms Kalshi order RTT, has the POLY
        # book moved below `shares`? Exit depth is logged too: a cost you can't fill at is a STRAND.
        # Detection probe (DRY-only): REST-verify each edge at FIRST SIGHT, before the 0.3s
        # persistence gate can kill it. Breaks a catch-22 — would_fire/interleg/exec_order only log
        # post-gate survivors, so the data needed to judge the gate could only be collected by
        # removing it. Join to rejected_edges.csv (`not_persistent` carries `lived Xs`) to ask: of
        # the edges the gate killed, how many were REAL at t=0?
        _dtp_path = "logs/detect_probe.csv"
        # Versioned, NOT the raw open()+header-if-new this used to be: that pattern writes the
        # header only when the file is new, so a column add appends wider rows under the stale
        # on-disk header — the exact ragged-row corruption _open_versioned_csv exists to prevent,
        # and it would have landed silently on the t0_rest_kalshi_ask add. (exec_order_probe below
        # still carries the raw pattern — same latent exposure, not touched here.)
        self._detect_probe_file, self._detect_probe_csv = _open_versioned_csv(
            _dtp_path, [
                "timestamp", "event", "kalshi_ticker", "kalshi_side", "shares", "ws_edge",
                "poly_ask_raw", "poly_limit", "t0_live_poly_ask", "t0_poly_state",
                "t0_poly_fillable",
                "kalshi_ask", "kalshi_limit", "t0_entry_depth",
                # The ticker's error, measured rather than inferred: t0_entry_depth=0 says the
                # ticker was wrong, t0_rest_kalshi_ask says BY HOW MUCH. t0_ticker_err =
                # rest_ask - kalshi_ask; >0 = stale-LOW = the phantom direction. All three blank
                # (never 0) when the book was unread — see _kalshi_ask_from_book's three states.
                "t0_rest_kalshi_ask", "t0_rest_ask_qty", "t0_ticker_err",
                "t0_poly_ok", "t0_kalshi_ok", "t0_real", "read_ms",
            ], "rest-ask")

        _eop_path = "logs/exec_order_probe.csv"
        _eop_new = not os.path.exists(_eop_path) or os.path.getsize(_eop_path) == 0
        self._exec_order_file = open(_eop_path, "a", newline="", buffering=1)
        self._exec_order_csv = csv.writer(self._exec_order_file)
        if _eop_new:
            self._exec_order_csv.writerow([
                "timestamp", "event", "kalshi_ticker", "kalshi_side", "shares", "edge",
                # what unwinding the KALSHI leg would cost (mirrors _unwind_kalshi_excess)
                "kalshi_ask", "kalshi_bid", "kalshi_sell_px", "kalshi_unwind_cost_per_share",
                "kalshi_exit_depth",
                # what flattening the POLY leg would cost (mirrors sell_back)
                "poly_ask_raw", "poly_exit_px", "poly_flatten_cost_per_share", "poly_exit_depth",
                # kalshi_first's mirror of §5 (delay_ms is MEASURED elapsed — segment on it)
                "poly_fillable_t0", "poly_fillable_t1", "delay_ms", "poly_would_kill",
            ])

        # Fill-success observability: one row per detected edge that reaches the fire-path fresh-REST
        # re-read — whether BOTH legs are fillable at intended price+size when the re-read lands (the
        # verdict-question dataset; see kalshi_arb._log_fill_success). Own fs_id sequence, seeded from
        # on-disk so a restart doesn't reset it; same versioned-open + blank-not-zero discipline as
        # would_fire. NOTE: the both_fill rate from this is an OPTIMISTIC upper bound (the re-read is
        # cheaper than placing an order → omits the ~30ms order RTT) — read per the plan.
        _fs_path = "logs/fill_success.csv"
        self._fs_seq = _max_id_col0(_fs_path)
        self._fs_csv_file, self._fs_csv = _open_versioned_csv(
            _fs_path,
            [
                "fs_id", "timestamp", "event", "poly_token", "kalshi_ticker", "kalshi_side",
                "outcome", "target_shares", "edge",
                "poly_ask_raw", "poly_limit", "live_poly_ask", "poly_slip",
                # *_shares_short = the UNFILLED SHORTFALL = max(0, target_shares − *_fillable),
                # NOT shares filled. On a *_moved row the leg had zero fillable depth, so the
                # shortfall equals the whole target (nothing filled) — the opposite of "filled".
                "poly_fillable", "poly_shares_short", "poly_state",
                "kalshi_ask", "kalshi_limit", "kalshi_fillable", "kalshi_shares_short",
                "fill_window_ms", "poly_read_latency_ms", "rest_transact_age_s", "minutes_since_kickoff",
            ],
            "v1",
        )

        # Phase 2.5 execution economics: three buckets (execution_cost = realized
        # flatten/strand losses, the trusted one; marked_unsettled = booked hedge edge;
        # realized_settled = post-settlement, captured later). The kill (safety.
        # is_exec_cost_cap_hit) sums realized losses CUMULATIVELY from this CSV, so a bad
        # run can't fund itself on optimistic edge — independent of marked_unsettled.
        _pnl_path = "logs/execution_pnl.csv"
        _pnl_new = not os.path.exists(_pnl_path) or os.path.getsize(_pnl_path) == 0
        self._exec_pnl_file = open(_pnl_path, "a", newline="", buffering=1)
        self._exec_pnl_csv = csv.writer(self._exec_pnl_file)
        if _pnl_new:
            self._exec_pnl_csv.writerow([
                "timestamp", "bucket", "kind", "event", "kalshi_ticker", "qty", "amount", "detail",
            ])

        # The ONE live writer of the realized_settled bucket: scores executed hedges
        # (logs/trades.log) against actual settlement and records true post-settlement P&L
        # (incl. void/divergence losses) so the cumulative-loss cap can see them. Reads
        # trades.log only (never the tracker); inert in DRY (no live hedges to score).
        self._settlement_scorer = SettlementScorer(
            self.client, self.kalshi_client, self._exec_pnl_csv,
            w_void=config.SETTLEMENT_W_VOID,
        )

        # Fat-spike diagnostic: on a >5% edge, a short burst of WS-vs-REST price+depth samples on
        # both venues (logs/fat_spike_samples.csv) so a fat edge can later be classified WS-ghost /
        # real-but-sub-window / observed-late. Pure observability; never feeds a gate or the loss
        # cap — the guard hard-bars writing it as a backtest/loss-cap file. One row per burst sample.
        _fat_spike_path = "logs/fat_spike_samples.csv"
        _assert_fat_spike_path(_fat_spike_path)
        # poly_transact_age_s / kalshi_ws_age_s appended LAST (the WS-vs-REST diagnostic columns
        # 0..23 keep their positions, which test_fat_spike pins by index). poly_transact_age_s =
        # the REST book's own server-side staleness (now − marketData.transactTime); kalshi_ws_age_s
        # = the frozen WS ask's age — so every sampled book carries a freshness signal, not just an
        # offset-from-detection.
        self._fat_spike_csv_file, self._fat_spike_csv = _open_versioned_csv(
            _fat_spike_path,
            [
                "timestamp", "arb_id", "event", "poly_token", "kalshi_ticker", "kalshi_side",
                "edge_pct", "sample_idx",
                "poly_ws_ask", "poly_ws_depth", "poly_offset_ms", "poly_latency_ms", "poly_status",
                "poly_rest_ask", "poly_rest_depth", "poly_state",
                "kalshi_ws_ask", "kalshi_ws_depth", "kalshi_offset_ms", "kalshi_latency_ms",
                "kalshi_status", "kalshi_rest_ask", "kalshi_rest_depth", "kalshi_rest_depth_at_ws_ask",
                "poly_transact_age_s", "kalshi_ws_age_s",
                "poly_open_interest", "poly_oi_age_s", "poly_last_trade_px", "poly_last_trade_qty",
                "poly_last_trade_age_s", "poly_shares_traded", "poly_notional_traded",
                "kalshi_open_interest", "kalshi_volume", "kalshi_last_trade_px", "kalshi_liq_age_s",
                "minutes_since_kickoff",
            ],
            "liquidity",
        )

    _RESUME_FLAG = "logs/resume"
    _HALT_CHECK_INTERVAL = 5.0

    def _trading_halted(self) -> bool:
        """Gate fronting ALL live order placement: True when the kill switch
        (`pause.json`) is present or today's net P&L has breached DAILY_LOSS_LIMIT.

        Cached and re-evaluated at most every _HALT_CHECK_INTERVAL seconds — the
        loss-cap check reads trades.log, so it must not run on every execution
        tick. Kill-switch latency is therefore up to that interval; acceptable
        for "stop taking NEW trades" (in-flight legs are already reconciled)."""
        now = time.time()
        if now - self._last_halt_check >= self._HALT_CHECK_INTERVAL:
            self._last_halt_check = now
            self._halted = (
                is_paused() or is_daily_loss_cap_hit() or is_exec_cost_cap_hit()
            )
        return self._halted

    async def _strand_control_loop(self) -> None:
        """A strand globally pauses trading. Instead of failing silently, alert
        ONCE (Discord @everyone + phone) with the choice, and watch for a resume
        signal. Clear & resume: `touch logs/resume`. Stop & examine: Ctrl-C.
        Works unattended (no blocking stdin prompt that would hang on the box)."""
        alerted = False
        while True:
            await asyncio.sleep(2)
            if not self.tracker.has_stranded():
                alerted = False
                if os.path.exists(self._RESUME_FLAG):
                    try: os.remove(self._RESUME_FLAG)   # stale flag, no strand
                    except OSError: pass
                continue
            if not alerted:
                alerted = True
                stranded = [p for p in self.tracker.list_positions() if p.is_stranded]
                details = "; ".join(
                    f"{p.event_title}: {p.shares:.0f} of {p.token_id[:24]} (${p.cost_basis:.2f})"
                    for p in stranded
                ) or "(see logs)"
                log.critical(
                    f"🚨 BOT PAUSED on stranded leg. CLEAR & RESUME: touch "
                    f"{self._RESUME_FLAG}  |  STOP & EXAMINE: Ctrl-C"
                )
                send_strand_pause_alert(details, self._RESUME_FLAG)
            if os.path.exists(self._RESUME_FLAG):
                try: os.remove(self._RESUME_FLAG)
                except OSError: pass
                n = self.tracker.clear_stranded()
                log.warning(f"▶️  Resume requested: cleared {n} stranded record(s) — resuming trading")
                alerted = False

    async def start(self):
        """Boots the background tasks and ties the feed to the detector."""
        # Wire the price-update callback to the active Poly feed so US mode gets
        # WS-speed arb detection (not just the 1s fallback loop).
        if self._poly_us:
            self._poly_us_feed.set_callback(self._on_price_update)
        else:
            self.feed.set_callback(self._on_price_update)
        self.kalshi_feed.set_callback(self._on_kalshi_price_update)
        # Read the WS-loop-timing flags once (default off → the probe task no-ops and the
        # feeds skip recording). Diagnostic only; never touches detection/cache/fire.
        ws_timer.configure()
        await self._validate_macro_pairs_at_startup()

        poly_feed_task = (
            self._poly_us_feed.run_forever() if self._poly_us else self.feed.run_forever()
        )
        await asyncio.gather(
            poly_feed_task,
            self.kalshi_feed.run_forever(),
            self._market_discovery_loop(),
            self._kalshi_discovery_loop(),
            self._kalshi_price_refresh_loop(),
            self._kalshi_arb_loop(),
            self._macro_arb_loop(),
            self._peak_balance_loop(),
            self._memory_watch_loop(),
            self._strand_control_loop(),
            # Venue-vs-tracker reconciliation: the tracker is a LOCAL BELIEF, and
            # _strand_control_loop only ever inspects that belief — so a position we
            # failed to record (lost order response / crash mid-execution) is invisible
            # to it. This loop asks the VENUES what we actually hold.
            self._reconcile_loop(),
            self._settlement_scorer.loop(),
            run_loop_lag_probe(),
        )
