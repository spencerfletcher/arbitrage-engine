"""
bot/config.py
─────────────
Loads all configuration from environment variables (via .env file).

NOTE (public snapshot): the numeric defaults below are illustrative and, where they
encode calibration, deliberately conservative. Production values — position sizing,
loss caps, and thresholds tuned from private measurement — are supplied at runtime via
`.env` (never committed) and are not represented by these defaults. The rationale
comments are kept because the *reasoning* is the point; the exact tuned values are not.
See NOTICE.md for the public/private split.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    """Return env var value or raise a descriptive error."""
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Copy .env.example to .env and fill in your values."
        )
    return val


def _optional(key: str, default: str = "") -> str:
    """Return env var value or a default (no error if missing)."""
    return os.getenv(key, default)


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("1", "true", "yes")


def _int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


def _float(key: str, default: float = 0.0) -> float:
    return float(os.getenv(key, str(default)))


# ── Wallet ────────────────────────────────────────────────────────────────────
# These are intentionally optional at module import time so that unit tests
# can import bot modules without a .env file. They will be validated by
# validate_trading_config() before any real order is placed.
PRIVATE_KEY: str = _optional("PRIVATE_KEY")
FUNDER_ADDRESS: str = _optional("FUNDER_ADDRESS")
SIGNATURE_TYPE: int = _int("SIGNATURE_TYPE", 0)

# ── Polymarket venue selection ──────────────────────────────────────────────
# "global" = polymarket.com CLOB (py-clob-client, wallet auth)
# "us"     = polymarket.us  (polymarket-us SDK, API-key auth, CFTC-regulated)
# ⚠️ DEFAULT IS "global" (legacy) — production runs "us" via .env. Assume "us" unless you've
#    confirmed otherwise; the "global" CLOB is geoblocked (403) from the prod server anyway.
POLY_VENUE: str = _optional("POLY_VENUE", "global").lower()
POLYMARKET_US_KEY_ID: str = _optional("POLYMARKET_US_KEY_ID")
POLYMARKET_US_SECRET_KEY: str = _optional("POLYMARKET_US_SECRET_KEY")
# Poly US sport series IDs to scan (from sports.list(): nba=4, nhl=6, mlb=15, World Cup=69,
# wnba=49). Chosen to overlap with KALSHI_SERIES. Override with a comma-separated env var.
POLYMARKET_US_SERIES: list[str] = [
    s.strip()
    for s in _optional("POLYMARKET_US_SERIES", "69,15,4,6,49").split(",")
    if s.strip()
]
# Transport for the Poly US live price feed: "raw" (our own JSON websocket: explicit
# ping/pong + heartbeat-stop reconnect; default) or "sdk" (polymarket-us SDK WebSocket;
# its is_connected can lie on a zombie socket — once froze prices 11 min and produced
# phantom edges). Raw validated live 2026-06-19 (sustained 400–800 book changes/min vs
# the SDK's repeated 45s freezes); set "sdk" in .env to fall back. Raw requires pynacl.
POLY_US_FEED_SOURCE: str = _optional("POLY_US_FEED_SOURCE", "raw").lower()


def validate_trading_config() -> None:
    """Raise EnvironmentError if wallet credentials are missing.
    Call this once at bot startup (before placing any orders)."""
    for key in ("PRIVATE_KEY", "FUNDER_ADDRESS"):
        _require(key)

# ── Bot behaviour ─────────────────────────────────────────────────────────────
DRY_RUN: bool = _bool("DRY_RUN", True)
# ⚠️ DEAD — ZERO readers anywhere in bot/. Discovery is hardcoded 300s (poly_arb.py,
# ladder.py). Kept only so an existing .env doesn't look wrong; it does nothing.
POLL_INTERVAL_SECONDS: int = _int("POLL_INTERVAL_SECONDS", 45)
MAX_POSITION_USD: float = _float("MAX_POSITION_USD", 500.0)
# CROSS_ARB_SIZE_TIERS + _edge_size_multiplier DELETED 2026-07-15. The breakpoints
# (0.005/0.01) were sportsbook-era and sat BELOW the live fire floor KALSHI_ARB_MIN_EDGE=0.02,
# so the multiplier was a constant 1.0 on every live path — while CLAUDE.md, README and
# position_management.md all credited it for the 5-10 share ramp. The ramp is MAX_POSITION_USD
# alone; raising it 5->500 is an unmodulated 100x step with nothing to damp it.
# Don't re-add edge-scaled sizing without re-tuning ABOVE the floor, and never size UP on fat
# edges: cross-venue a fat edge means the venues DISAGREE (one is stale) = peak strand risk.
# See docs/TODO.md S1.
# Real arb from liquid markets rarely exceeds 15% edge.
# A larger edge almost always means a stale price on an illiquid book.
# ⚠️ BOTH DEAD on the live path — they READ as risk limits and are not.
# MAX_ARB_EDGE: only reader is the DORMANT polymarket/arb_detector.py. The live implausible-
#   edge ceiling is KALSHI_MAX_PLAUSIBLE_EDGE (0.50) — 3.3x HIGHER. Setting this to 0.15
#   enforces nothing; an operator would reasonably think it caps the live path.
# MIN_LIQUIDITY_SHARES: only reader is the DORMANT polymarket/sizer.py. The live depth gate
#   is poly_fillable / _rest_fillable vs shares (fresh REST, per-fire).
MAX_ARB_EDGE: float = _float("MAX_ARB_EDGE", 0.15)
MIN_LIQUIDITY_SHARES: float = _float("MIN_LIQUIDITY_SHARES", 100.0)
# Max total USD deployed across ALL trades in a single scan cycle
MAX_TOTAL_EXPOSURE: float = _float("MAX_TOTAL_EXPOSURE", 2000.0)
# Cooldown: don't re-enter the same event within this many seconds
EVENT_COOLDOWN_SECONDS: int = _int("EVENT_COOLDOWN_SECONDS", 300)
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()



# ── Kalshi (second prediction market — fully automated cross-platform arb) ────
KALSHI_API_KEY: str = _optional("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH: str = _optional("KALSHI_PRIVATE_KEY_PATH", "")
# ⚠️ DEFAULT IS "demo" — production MUST set KALSHI_ENV=prod in .env (else it trades the demo venue).
KALSHI_ENV: str = _optional("KALSHI_ENV", "demo")   # "demo" or "prod"
KALSHI_SERIES: list[str] = [
    s.strip()
    for s in _optional("KALSHI_SERIES", "KXNBAGAME,KXNHLGAME,KXMLBGAME,KXWCGAME,KXWNBAGAME").split(",")
    if s.strip()
]
# Min edge to fire a cross-venue arb. This is the REAL divergence-tail gate: a bad
# settlement divergence loses ~the full stake regardless of price, so break-even
# divergence rate ≈ the edge itself. 0.02 → only take edges that survive a ~2%
# ambiguous-settlement rate (~2x margin over the ~0.9% rate research flagged), and it
# still captures real edges like the Cleveland 2.2%. Server .env should match (0.02).
KALSHI_ARB_MIN_EDGE: float = _float("KALSHI_ARB_MIN_EDGE", 0.02)
# Implausible-edge (cross-venue PRICE-divergence) sanity ceiling — reject any edge above this as a
# phantom (one venue's price is stale/wrong; a real arb is a small, cents-scale edge). Empirical-
# WITH-HEADROOM, NOT a proven bound: largest real edge observed ~0.33 on thin data, freeze-recovery
# phantoms cluster 0.68-0.75; 0.50 sits clear of both. Tune DOWN as phantom data accrues. (Distinct
# from KALSHI_ARB_MIN_EDGE, the lower bound, and from the settlement-divergence void tail.)
KALSHI_MAX_PLAUSIBLE_EDGE: float = _float("KALSHI_MAX_PLAUSIBLE_EDGE", 0.50)

# ⚠️ DEAD 2026-07-15 — the ladder EXECUTION path (bot/runner/ladder.py) was deleted. A
# single-venue BTC arb in a sports cross-arb bot: it never found an arb, was wired live
# with no on/off flag, had ZERO test coverage of its executor, and carried two silent
# naked-position bugs (unwind response discarded; a partial fill classified as "neither
# leg filled" -> position held, unrecorded, invisible to the exposure cap and
# has_stranded). Subscribing these series also neutered the frozen-book watchdog.
# The KALSHI_LADDER_* knobs are removed; a stale one in .env is simply ignored.
# bot/kalshi/ladder.py survives for classify_market, which macro_pairs uses.
KALSHI_MACRO_SERIES: list[str] = [
    s.strip()
    for s in _optional("KALSHI_MACRO_SERIES", "KXFED,KXCPI").split(",")
    if s.strip()
]
KALSHI_MACRO_MIN_EDGE: float = _float("KALSHI_MACRO_MIN_EDGE", 0.01)
# Minimum-profit floor on the KALSHI limit — the slice of the edge we refuse to give up.
# ⚠️ RAISING THIS TIGHTENS, IT DOES NOT LOOSEN. Kalshi's limit is breakeven-MINUS-buf, so a bigger
# buf demands more profit and fills LESS often. It buys no room to fill through a fast move.
# Not a Poly knob and not "2 × this" of joint tolerance: the Poly leg bids the raw ask flat since
# S4 (2026-07-16, its cushion rescued 0/14 measured misses), and the 2×buf model was wrong before
# that (corrected 2026-07-15). Kalshi is now the ONLY reader. See _fok_buffer's docstring.
KALSHI_FOK_BUFFER: float = _float("KALSHI_FOK_BUFFER", 0.005)
# Edge-proportional allowance: buf = max(KALSHI_FOK_BUFFER, edge * fraction / 2), capped at
# 0.45*edge. ⚠️ It does NOT make fat-edge spikes "fill through fast moves" on the Kalshi leg — a
# bigger buf TIGHTENS that limit (see _fok_buffer). 0 = fixed floor only.
KALSHI_FOK_EDGE_FRACTION: float = _float("KALSHI_FOK_EDGE_FRACTION", 0.35)
# Max age (seconds) for a cached price before execution is skipped. Prevents acting
# on stale prices when the WS is quiet. Default matches the REST refresh interval.
KALSHI_MAX_PRICE_AGE_SECONDS: int = _int("KALSHI_MAX_PRICE_AGE_SECONDS", 30)
# Source for Kalshi prices: "ticker" (top-of-book quote channel, can lag the real
# book during fast moves → phantom edges) or "orderbook" (real executable book via
# orderbook_delta). Default "ticker" until orderbook pricing is validated live.
KALSHI_PRICE_SOURCE: str = _optional("KALSHI_PRICE_SOURCE", "ticker").lower()
# Orderbook-mode integrity check: if the WS-maintained best yes-bid drifts from
# the REST book by more than this (dollars), the local book has a stale level →
# force a resnapshot. The REST poll does the comparison off the hot path.
KALSHI_BOOK_MAX_DIVERGENCE: float = _float("KALSHI_BOOK_MAX_DIVERGENCE", 0.03)
# Min seconds between divergence-triggered resnapshots (each re-subscribes ALL book
# tickers). Without this, a single chronically-divergent ticker triggers a resnapshot
# every audit → a storm that floods the WS and causes more seq gaps. The divergent
# ticker stays suspect (untradeable) regardless; we just stop re-snapshotting for it.
KALSHI_RESNAP_THROTTLE_SECONDS: float = _float("KALSHI_RESNAP_THROTTLE_SECONDS", 30.0)
# Seconds between REST polls (price refresh in ticker mode; book-integrity audit in
# orderbook mode). Lower = stale book levels heal faster. Token cost is trivial.
KALSHI_AUDIT_INTERVAL: int = _int("KALSHI_AUDIT_INTERVAL", 3)

# ── WS receive-loop timing diagnostics (default OFF — zero cost when off) ──────────
# When True, instruments the Poly US + Kalshi WS receive loops with per-message inline
# handler time (parse+cache+callback-scheduling), inter-arrival gap, and rolling msg rate,
# plus an independent event-loop-lag probe — to decide whether the loop keeps up under
# game-time load. Sampled 1-in-N and buffered, flushed to logs/ws_loop_timing.csv (a
# SEPARATE diagnostic file; never an existing log). Pure observability: detection/cache/
# fire logic is byte-for-byte identical on or off. See bot/core/ws_timing.py.
WS_LOOP_TIMING: bool = _bool("WS_LOOP_TIMING", False)
WS_LOOP_TIMING_SAMPLE_N: int = _int("WS_LOOP_TIMING_SAMPLE_N", 1)   # buffer 1 row per N msgs
WS_LOOP_TIMING_FLUSH_N: int = _int("WS_LOOP_TIMING_FLUSH_N", 200)   # flush buffer every N rows
# Skip arbs whose Poly leg is a longshot priced below this. A 5¢ leg means the other
# leg is 95¢: if the cheap leg misses, the expensive leg strands — the asymmetry that
# turns a miss into a big loss. Higher = safer, fewer opportunities.
KALSHI_MIN_POLY_PRICE: float = _float("KALSHI_MIN_POLY_PRICE", 0.10)
# Persistence confirm: an edge must stay >= KALSHI_ARB_MIN_EDGE continuously for this
# many seconds before the bot fires. Filters sub-second goal-spike phantoms; real
# multi-second edges still fire. (NOT proof of profit — see settlement gate + backtest.)
KALSHI_CONFIRM_SECONDS: float = _float("KALSHI_CONFIRM_SECONDS", 1.5)
# §5 inter-leg-window probe (DRY-only, observability): after a would-fire the probe waits
# this long — a STAND-IN for the real poly-fill→kalshi-fire round-trip — then re-reads the
# Kalshi leg's fillable depth to measure how often it would have dropped below `shares` in
# the window (a simulated FOK kill). Touches no decision. See docs/TODO.md §5.
# CALIBRATED 2026-06-26 to in-region latency from scripts/latency_probe.py + TCP/origin-read
# probes: network leg to the Poly ORDER host (api.polymarket.us) is ~1ms (sub-ms TCP, colocated);
# the Poly origin app round-trip (cf=MISS read) is ~22ms. NO script captures the order place→fill
# path, so the true order RTT is ≥ that + matching-engine time — confirmable only via live
# fills.log. **50ms CONFIRMED CORRECT [VERIFIED 2026-07-14 via scripts/order_rtt_probe on PROD]** —
# do NOT "fix" it to 61. The real Poly ORDER place→engine→response RTT (this constant's calibration
# target: poly_first fires Poly, then Kalshi, so the window ≈ the Poly order RTT) measured
# median 61ms (min 37, max 89, n=6). This constant is the SLEEP, not the modelled window: the probe
# sleeps it, then reads the book, so the book's server-side state lands at sleep + one-way (~9ms;
# the read's full RTT is ~18ms, visible as the logged delay_ms ≈ 65-76 at sleep=50). Effective window
# ≈ 50 + 9 = ~59ms vs the 61ms target — within 3%. The prior guess (22ms read RTT + cushion) was good.
#   Trap: `delay_ms` in interleg_probe.csv is the MEASURED elapsed (sleep + read RTT), NOT this
#   constant — segment the log on it, don't assume. Rows at delay_ms ≈ 265-278 are the OLD 250ms
#   placeholder era (a ~4x too-long window → they over-count kills); rows at ≈ 65-90 are this era.
#   Any §5 rate pooled across both is a mongrel — filter to the 65-90 band.
#   Kalshi's own order RTT is 17ms (min 14, max 24) — 3.6x faster than Poly, which is why
#   kalshi_first would carry a ~3.6x SHORTER inter-leg window (see the TODO execution-order item).
KALSHI_INTERLEG_PROBE_DELAY_MS: int = _int("KALSHI_INTERLEG_PROBE_DELAY_MS", 50)
# Book-trust gate: after a seq gap / REST divergence / resnapshot, treat the affected
# ticker(s) as untrustworthy for this many seconds (no trading until the book restabilizes).
# Tune after observing the server's gap rate (Phase 4.5) — on a gappy feed this ≈ halt.
KALSHI_BOOK_SUSPECT_SECONDS: float = _float("KALSHI_BOOK_SUSPECT_SECONDS", 5.0)

# ── Settlement-backtest pre-committed gate (scripts/settlement_backtest.py) ──────────
# Falsification harness constants — fixed BEFORE looking at data. The gate can only
# KILL / return PROVISIONAL / INSUFFICIENT — never "deploy."
SETTLEMENT_MIN_HEDGED_N: int = _int("SETTLEMENT_MIN_HEDGED_N", 30)   # on POST-haircut count
SETTLEMENT_F_PASS: float = _float("SETTLEMENT_F_PASS", 0.3)          # PASS requires net(F_PASS)>0
# Tail RATES are PLACEHOLDERS pending grounding from tournament history (count WC/major
# group-stage voids over last N tournaments; objective-scoreline divergences rarer still).
# Deliberately NOT the old 0.02/0.005 (an order too high → false-fail). The backtest prints
# break-even P_DIV / W_VOID so you see how close the verdict is to flipping on these. STAMP
# with source+date when grounded.
SETTLEMENT_P_VOID: float = _float("SETTLEMENT_P_VOID", 0.005)        # PLACEHOLDER — ground from history
SETTLEMENT_P_DIV: float = _float("SETTLEMENT_P_DIV", 0.002)          # PLACEHOLDER — ground from history
# Per-share $ wedge between Poly LFMP and Kalshi fair-price marks on a void. The single
# UNOBSERVED constant (never held a voided position) — flagged as the weakest in the report.
SETTLEMENT_W_VOID: float = _float("SETTLEMENT_W_VOID", 0.10)         # ASSUMPTION, unobserved
# Tiny-live blessing thresholds (Gate 3, runtime — NOT the backtest): size up only after
# ≥N_TL hedged fills with cumulatively positive realized_settled across ≥M_TL matches.
SETTLEMENT_TINY_LIVE_N: int = _int("SETTLEMENT_TINY_LIVE_N", 20)
SETTLEMENT_TINY_LIVE_MATCHES: int = _int("SETTLEMENT_TINY_LIVE_MATCHES", 5)
# Origin-frozen-book age threshold (seconds). A Poly book whose server transactTime is older
# than this is treated as a stale-book phantom, NOT a live quote: the verdict (scripts/
# subsecond_calibration.py + scripts/data_audit.py) EXCLUDES such both_fills from the
# settled∩both_fill denominator (a frozen-book fill is a manufactured edge, same defect class
# as a kalshi_fillable=0 phantom). 30 matches the Poly CDN max-age. SINGLE source of truth:
# the G1 fire-path freshness gate (when built) MUST import THIS constant so "fired" and
# "counted" use the same boundary — never a second literal. See docs/verdict_methodology.md.
FROZEN_BOOK_AGE_S: float = _float("FROZEN_BOOK_AGE_S", 30.0)
# Origin-freeze fire-gate (G1): the firing book being stale (age > FROZEN_BOOK_AGE_S) is
# AMBIGUOUS alone (frozen vs legitimately-illiquid — the §5 trap), so it's only a FLAG. The
# non-ambiguous discriminator is CROSS-MARKET: an origin freeze stalls many books at once. Reject
# a freeze-suspect fire only when at least this many OTHER tracked markets are simultaneously
# stale; a LONE stale book is NOT rejected (preserve the real illiquid-edge tail). See
# bot/runner/kalshi_arb.py origin_freeze_suspect + bot/poly_us/feed.py count_stale_books.
ORIGIN_FREEZE_MIN_PEERS: int = _int("ORIGIN_FREEZE_MIN_PEERS", 2)
# Execution order. "poly_first" (default): fire the liquid Poly leg first, then the
# Kalshi FOK leg sized to the ACTUAL Poly fill; a Kalshi miss unwinds Poly (cheap),
# never strands the illiquid leg. "kalshi_first": fire Kalshi FOK first (zero cost if
# it kills), then complete Poly. Switchable for the live flatten-vs-strand A/B.
KALSHI_EXEC_ORDER: str = _optional("KALSHI_EXEC_ORDER", "poly_first").lower()
# Hard kill: if CUMULATIVE realized loss (flatten/strand cost + settled void/divergence
# losses, from execution_pnl.csv) exceeds this dollar budget, halt — independent of any
# booked/marked edge, monotonic, no auto-resume. See safety.is_exec_cost_cap_hit. 0 = off.
KALSHI_EXEC_COST_BUDGET: float = _float("KALSHI_EXEC_COST_BUDGET", 0.0)
KALSHI_PNL_KILL_WINDOW: int = _int("KALSHI_PNL_KILL_WINDOW", 3600)  # UNUSED: cap is cumulative, not windowed

# Commission-charging exchanges (Matchbook, Betfair, …) quote raw odds, so a
# 0.5% gross edge can still be a loss after commission. Require this much
# post-commission edge before acting on an opportunity sourced from such a
# book. Used as max(CROSS_ARB_MIN_EDGE, this) per-book in _try_direction.
CROSS_ARB_MIN_EDGE_COMMISSION: float = _float("CROSS_ARB_MIN_EDGE_COMMISSION", 0.015)

# ── API endpoints ─────────────────────────────────────────────────────────────
CLOB_HOST: str = "https://clob.polymarket.com"
GAMMA_HOST: str = "https://gamma-api.polymarket.com"
CHAIN_ID: int = 137  # Polygon mainnet

# ── Daily loss cap ────────────────────────────────────────────────────────────
# Maximum net loss (USD) allowed per UTC day before the bot stops taking new
# trades. Computed from guaranteed_profit in trades.log. 0 = disabled.
DAILY_LOSS_LIMIT: float = _float("DAILY_LOSS_LIMIT", 0.0)

# ── Kill switch ───────────────────────────────────────────────────────────────
# Path to a file the operator can create to pause trade execution without
# killing the process.  `touch pause.json` pauses; `rm pause.json` resumes.
KILL_SWITCH_FILE: str = _optional("KILL_SWITCH_FILE", "pause.json")

# ── Notifications ─────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL: str = _optional("DISCORD_WEBHOOK_URL")
# Separate channel for "proper edge" alerts: every would-fire (an edge that passed
# EVERY gate — fillable + fresh, not a phantom). Falls back to the main webhook if
# unset; set to a distinct channel's webhook to keep the edge feed clean.
DISCORD_EDGE_WEBHOOK_URL: str = _optional("DISCORD_EDGE_WEBHOOK_URL")
# Whether to send Discord notifications even when DRY_RUN is true
DISCORD_NOTIFY_DRY_RUN: bool = _bool("DISCORD_NOTIFY_DRY_RUN", False)
# Pushover push notifications (phone alerts for urgent arbs)
PUSHOVER_USER_KEY: str = _optional("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN: str = _optional("PUSHOVER_API_TOKEN")

# ── Venue-vs-tracker position reconciliation ────────────────────────────────────────────────
# The tracker is a LOCAL BELIEF assembled from order responses — and everything keys off it
# (the strand global-pause, the alert, the exposure caps). Nothing used to ask either venue what
# we ACTUALLY hold, so a lost order response (30s httpx timeout, 502, connection reset AFTER the
# engine filled) or a crash between fill and add_position left a real position that was invisible
# to us: no alert, no pause, no cap. This loop closes that blind spot.
RECONCILE_ENABLED: bool = _bool("RECONCILE_ENABLED", True)
RECONCILE_INTERVAL_S: int = _int("RECONCILE_INTERVAL_S", 60)
# Kalshi's portfolio GET is eventually consistent (it can 404 right after a create), so ONE
# divergent poll proves nothing. Escalate only after this many CONSECUTIVE divergent polls.
RECONCILE_CONFIRM_POLLS: int = _int("RECONCILE_CONFIRM_POLLS", 2)
# Operator-acknowledged positions ("this is also mine and I know about it"). NOT an ignore-list:
# entries record an EXACT (venue, market, qty), so they ADD to what we know we hold rather than
# punching a hole in the check — if an acked position later CHANGES, it is no longer what was
# acknowledged and alerts again. `touch logs/reconcile_ack` writes the current divergence here.
RECONCILE_WHITELIST_FILE: str = _optional("RECONCILE_WHITELIST_FILE", "logs/reconcile_whitelist.json")
RECONCILE_ACK_FLAG: str = _optional("RECONCILE_ACK_FLAG", "logs/reconcile_ack")

# ── kalshi_first mirror probe (§5's mirror) ─────────────────────────────────────────────────
# KALSHI_INTERLEG_PROBE_DELAY_MS models poly_first's window (fire Poly → 61ms → fire Kalshi, so
# the KALSHI book is exposed). This models kalshi_first's: fire Kalshi → 17ms → fire Poly, so the
# POLY book is exposed. 17 = the MEASURED Kalshi ORDER RTT (median 17ms, min 14, max 24,
# scripts/order_rtt_probe on prod 2026-07-14) — 3.6x shorter than Poly's 61ms.
# ⚠️ Same trap as its sibling: this is the SLEEP, not the modelled window. The probe sleeps it,
# THEN reads the book, so the logged `delay_ms` is the MEASURED elapsed (sleep + read RTT) and is
# what analysis must segment on — never assume rows equal this constant.
KALSHI_MIRROR_PROBE_DELAY_MS: int = _int("KALSHI_MIRROR_PROBE_DELAY_MS", 17)
