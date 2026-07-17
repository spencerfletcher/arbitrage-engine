"""Tests for scripts/subsecond_calibration.py — the pure helpers behind the §A/§B/§C reads
and the SHARED verdict-counting logic (also consumed by scripts/data_audit.py).

The load-bearing ones:
  • settled_triple_keys: the verdict numerator counts SETTLED capture rows only — counting
    PENDING as settled overcounts toward SETTLEMENT_MIN_HEDGED_N → a false go-readiness signal.
  • _game_key: the verdict-N is game-keyed; the derivation must collapse one physical game's
    triples (::short↔canonical poly + yes/no side) to ONE key WITHOUT over-collapsing two real
    games. Pinned on REAL triple shapes from fill_success.csv (a test on hand-built shapes can
    pass while the real derivation drifts — the ::short keying lesson).
  • verdict_counts: frozen-book both_fills (age > FROZEN_BOOK_AGE_S) are EXCLUDED from the
    denominator (stale-book phantom); blank age is excluded conservatively, never counted fresh.
"""
import scripts.subsecond_calibration as sub


def _cap(poly_token, ticker, outcome, side="yes"):
    return {"poly_token": poly_token, "kalshi_ticker": ticker, "kalshi_side": side,
            "outcome": outcome}


def _bf(token, ticker, side="yes", age="0.2"):
    return {"outcome": "both_fill", "poly_token": token, "kalshi_ticker": ticker,
            "kalshi_side": side, "rest_transact_age_s": age}


# ── settled_triple_keys: settled excludes pending, keyed on the full triple ─────────────────
def test_settled_triple_keys_excludes_pending():
    rows = [
        _cap("tokA", "KXMLBGAME-A-X", "clean", "yes"),
        _cap("tokB", "KXMLBGAME-B-Y", "void", "no"),
        _cap("tokC", "KXMLBGAME-C-Z", "divergence", "yes"),
        _cap("tokD", "KXMLBGAME-D-W", "pending", "no"),    # NOT settled — must be excluded
    ]
    keys = sub.settled_triple_keys(rows)
    assert keys == {("tokA", "KXMLBGAME-A-X", "yes"), ("tokB", "KXMLBGAME-B-Y", "no"),
                    ("tokC", "KXMLBGAME-C-Z", "yes")}
    assert ("tokD", "KXMLBGAME-D-W", "no") not in keys   # the pending game does not count


def test_settled_triple_keys_includes_side():
    # the join keys on side — a yes-side fill must not match a no-side settlement row
    rows = [_cap("tok", "KXMLBGAME-A-X", "clean", "yes")]
    keys = sub.settled_triple_keys(rows)
    assert ("tok", "KXMLBGAME-A-X", "yes") in keys
    assert ("tok", "KXMLBGAME-A-X", "no") not in keys


def test_settled_triple_keys_empty_and_all_pending():
    assert sub.settled_triple_keys([]) == set()
    assert sub.settled_triple_keys([_cap("t", "K-A-X", "pending")]) == set()


# ── _game_key: the catastrophic-correctness derivation, on REAL shapes ──────────────────────
def test_game_key_collapses_short_canonical_and_side_real_shapes():
    # The real CLE-CWS both_fill triples from logs/fill_success.csv: ONE physical game fanned
    # into 3 triples by the ::short↔canonical poly split AND the yes/no side. All → ONE game key.
    cle_cws = [
        _bf("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CLE", "yes"),
        _bf("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CWS", "no"),
        _bf("aec-mlb-cle-cws-2026-06-24",        "KXMLBGAME-26JUN241410CLECWS-CWS", "yes"),
    ]
    assert len({sub._game_key(r) for r in cle_cws}) == 1               # no UNDER-collapse
    assert sub._game_key(cle_cws[0]) == "KXMLBGAME-26JUN241410CLECWS"


def test_game_key_does_not_over_collapse_distinct_games_real_shapes():
    # Real shapes: CLE-CWS, PHI-WSH, and AZ-STL on TWO different dates (06-23 vs 06-24, same
    # teams — different games). The key must keep all four distinct (over-collapse would deflate N).
    rows = [
        _bf("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CLE", "yes"),
        _bf("aec-mlb-phi-wsh-2026-06-24::short", "KXMLBGAME-26JUN241845PHIWSH-PHI", "yes"),
        _bf("aec-mlb-az-stl-2026-06-23::short",  "KXMLBGAME-26JUN231945AZSTL-AZ",   "yes"),
        _bf("aec-mlb-az-stl-2026-06-24",         "KXMLBGAME-26JUN241945AZSTL-AZ",   "no"),
    ]
    assert len({sub._game_key(r) for r in rows}) == 4   # AZ-STL 06-23 ≠ 06-24


def test_game_key_failsafe_no_dash():
    assert sub._game_key({"kalshi_ticker": "NODASH"}) == "NODASH"   # own key, never merged
    assert sub._game_key({"kalshi_ticker": ""}) == ""


# ── verdict_counts: frozen exclusion + game granularity ─────────────────────────────────────
def test_verdict_counts_excludes_frozen_even_when_settled():
    # a frozen-book both_fill is excluded from settled∩both_fill EVEN IF its capture row settled
    both = [_bf("tokF", "KXMLBGAME-F-X", "yes", age="0.2"),     # fresh
            _bf("tokG", "KXMLBGAME-G-Y", "no", age="200.0")]    # frozen (>30s)
    settled = {("tokF", "KXMLBGAME-F-X", "yes"), ("tokG", "KXMLBGAME-G-Y", "no")}  # BOTH settled
    vc = sub.verdict_counts(both, settled)
    assert vc["n_frozen_rows"] == 1
    assert vc["n_fresh_rows"] == 1
    assert vc["n_settled_games"] == 1       # only the fresh game counts toward the floor
    assert vc["n_settled_triples"] == 1     # the frozen-but-settled triple is NOT counted


def test_verdict_counts_age_unknown_excluded_not_counted_fresh():
    # fail-direction: a both_fill with blank/missing age can't be CONFIRMED fresh → excluded
    # (and not mislabeled frozen). Never silently counted as fresh.
    both = [_bf("tokU", "KXMLBGAME-U-X", "yes", age=""),                 # blank
            {"outcome": "both_fill", "poly_token": "tokV",
             "kalshi_ticker": "KXMLBGAME-V-Y", "kalshi_side": "no"}]     # missing column
    settled = {("tokU", "KXMLBGAME-U-X", "yes"), ("tokV", "KXMLBGAME-V-Y", "no")}
    vc = sub.verdict_counts(both, settled)
    assert vc["n_unknown_rows"] == 2
    assert vc["n_fresh_rows"] == 0 and vc["n_frozen_rows"] == 0
    assert vc["n_settled_games"] == 0       # unknown age NOT counted as a fresh settled observation


def test_verdict_counts_game_granularity_collapses_same_game():
    # 3 fresh triples of ONE game (real CLE-CWS shapes), all settled → 1 settled GAME, 3 triples
    both = [
        _bf("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CLE", "yes"),
        _bf("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CWS", "no"),
        _bf("aec-mlb-cle-cws-2026-06-24",        "KXMLBGAME-26JUN241410CLECWS-CWS", "yes"),
    ]
    settled = sub.settled_triple_keys([
        _cap("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CLE", "clean", "yes"),
        _cap("aec-mlb-cle-cws-2026-06-24::short", "KXMLBGAME-26JUN241410CLECWS-CWS", "clean", "no"),
        _cap("aec-mlb-cle-cws-2026-06-24",        "KXMLBGAME-26JUN241410CLECWS-CWS", "clean", "yes"),
    ])
    vc = sub.verdict_counts(both, settled)
    assert vc["n_settled_triples"] == 3 and vc["n_settled_games"] == 1
    assert vc["n_triples"] == 3 and vc["n_games"] == 1


# ── verdict_counts: feed-wide freeze-episode exclusion (the recovery-phantom defense) ─────────
def test_verdict_counts_excludes_freeze_episode_window_even_when_fresh_and_settled():
    # A both_fill in the 2026-06-25 22:03-22:30 Poly freeze window with a FRESH age (0.4s) — the
    # wf 44/45 recovery-phantom shape that G2's age-rule does NOT catch. The window exclusion drops
    # it WHOLESALE, even if it settled.
    in_window = {"outcome": "both_fill", "poly_token": "aec-mlb-ath-sf-2026-06-25",
                 "kalshi_ticker": "KXMLBGAME-26JUN251545ATHSF-SF", "kalshi_side": "yes",
                 "rest_transact_age_s": "0.407", "timestamp": "2026-06-25T22:26:36Z"}
    settled = {("aec-mlb-ath-sf-2026-06-25", "KXMLBGAME-26JUN251545ATHSF-SF", "yes")}
    vc = sub.verdict_counts([in_window], settled)
    assert vc["n_freeze_excluded_rows"] == 1
    assert vc["n_fresh_rows"] == 0 and vc["n_settled_triples"] == 0 and vc["n_settled_games"] == 0


def test_verdict_counts_freeze_window_boundary_is_exact_not_greedy():
    # A fresh both_fill JUST outside the window (22:31:00Z, after the 22:30 end) is NOT excluded —
    # the boundary is exact (end-exclusive), not greedy, so it counts normally.
    just_out = {"outcome": "both_fill", "poly_token": "tokB", "kalshi_ticker": "KXMLBGAME-B-Y",
                "kalshi_side": "no", "rest_transact_age_s": "0.4",
                "timestamp": "2026-06-25T22:31:00Z"}
    vc = sub.verdict_counts([just_out], set())
    assert vc["n_freeze_excluded_rows"] == 0 and vc["n_fresh_rows"] == 1


# ── existing helpers (unchanged) ────────────────────────────────────────────────────────────
def test_bucket_ranges():
    assert sub._bucket(0.0) == "~0"
    assert sub._bucket(0.25) == "~0.25"
    assert sub._bucket(1.0) == "~1"
    assert sub._bucket(99.0) is None   # out of range → no bucket


def test_num_blank_is_none_not_zero():
    assert sub._num({"x": ""}, "x") is None      # blank ≠ measured zero
    assert sub._num({"x": "0"}, "x") == 0.0       # a real zero parses
    assert sub._num({}, "x") is None              # missing key
    assert sub._num({"x": "abc"}, "x") is None    # garbage
