"""Tests for the pre-committed falsification gate (scripts/settlement_backtest)."""
import math
import pytest

import scripts.settlement_backtest as sb
from bot.core import config


# ── _build_record: hand-computed values + RAISE-on-missing ──────────────────────

def _wf(**over):
    row = dict(wf_id="1", shares="10", poly_fillable="1000", kalshi_fillable="1000",
               poly_limit="0.5000", kalshi_limit="0.4500",
               poly_ask_raw="0.4800", kalshi_ask="0.4300")
    row.update(over)
    return row


def _samples(poly_bid="0.4700", offset="1.0", poly_ask="0.4800", kalshi_ask="0.4300"):
    return [dict(wf_id="1", offset_s=offset, poly_ask=poly_ask, poly_depth="1000",
                 poly_bid=poly_bid, kalshi_ask=kalshi_ask, kalshi_fillable="1000")]


def test_build_record_values(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_W_VOID", 0.10)
    r = sb._build_record(_wf(), _samples())
    assert r["s"] == 10
    # hedged_pnl = 10·(1−0.5−0.45) − 0.05·10·0.5·0.5 − ceil(0.07·10·0.45·0.55·100)/100
    #            = 0.5 − 0.125 − 0.18 = 0.195
    assert r["hedged_pnl"] == pytest.approx(0.195, abs=1e-6)
    # flatten = 10·(0.5−0.47) + 0.05·10·0.5·0.5 + 0.05·10·0.47·0.53
    assert r["flatten_cost"] == pytest.approx(0.30 + 0.125 + 0.05*10*0.47*0.53, abs=1e-6)
    assert r["stake_row"] == pytest.approx(9.5)
    assert r["loss_given_div"] == pytest.approx(9.5)        # 1.0·stake (windfall priced $0)
    assert r["loss_given_void"] == pytest.approx(10 * 0.10)  # s·W_VOID


def test_build_record_raises_on_missing_poly_bid():
    # No sampler poly_bid → flatten uncomputable → RAISE (never silent 0).
    with pytest.raises(ValueError, match="poly_bid"):
        sb._build_record(_wf(), [])


def test_build_record_raises_on_missing_required_field():
    bad = _wf(); del bad["poly_limit"]
    with pytest.raises(ValueError, match="poly_limit"):
        sb._build_record(bad, _samples())


def test_stale_flag_set_when_edge_collapses():
    # Later sample: poly_ask jumps 0.48→0.60 → implied edge collapses → stale.
    r = sb._build_record(_wf(), _samples(poly_ask="0.6000"))
    assert r["stale"] is True
    r2 = sb._build_record(_wf(), _samples(poly_ask="0.4800"))  # unchanged → not stale
    assert r2["stale"] is False


# ── _won_set: stale/worst-biased selection ──────────────────────────────────────

def _rec(pnl, flat=0.0, stale=True, s=1, stake=1.0):
    return {"s": s, "pp": 0.5, "kp": 0.5, "hedged_pnl": pnl, "flatten_cost": flat,
            "stake_row": stake, "stale": stale, "naive_hedged": True,
            "poly_fillable": 1000.0, "poly_depth_suspect": False, "suspect_reason": None,
            "loss_given_div": 1.0 * stake, "loss_given_void": s * 0.10}


def test_won_set_picks_stale_worst_never_best():
    H = [_rec(5, stale=False), _rec(4, stale=False), _rec(3, stale=False),
         _rec(2, stale=True), _rec(1, stale=True)]
    won, status = sb._won_set(H, 0.4)        # want = round(0.4·5) = 2
    pnls = sorted(r["hedged_pnl"] for r in won)
    assert pnls == [1, 2]                      # the two stale (lowest), not the 5/4/3
    assert status == "PRICED"
    assert 5 not in [r["hedged_pnl"] for r in won]  # never the best row


def test_won_set_unpriced_when_no_stale():
    H = [_rec(3, stale=False), _rec(1, stale=False), _rec(2, stale=False)]
    won, status = sb._won_set(H, 0.34)        # want = 1 → lowest-pnl fresh
    assert [r["hedged_pnl"] for r in won] == [1]
    assert "UNPRICED" in status


# ── _evaluate: each verdict path ────────────────────────────────────────────────

@pytest.fixture
def small_n(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_MIN_HEDGED_N", 1)
    monkeypatch.setattr(config, "SETTLEMENT_P_VOID", 0.0)
    monkeypatch.setattr(config, "SETTLEMENT_P_DIV", 0.0)


def test_verdict_insufficient_data(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_MIN_HEDGED_N", 100)
    r = sb._evaluate([_rec(1.0) for _ in range(10)])   # 0.3·10=3 < 100
    assert r["verdict"] == "INSUFFICIENT DATA"


def test_verdict_clean_pass(small_n):
    r = sb._evaluate([_rec(1.0, flat=0.1) for _ in range(10)])   # before(0.3)=3−0.7=2.3, tails 0
    assert r["verdict"] == "PROVISIONAL PASS"


def test_verdict_kill_pre_tails(small_n):
    r = sb._evaluate([_rec(0.1, flat=1.0) for _ in range(10)])   # before(0.3)=0.3−7<0
    assert r["verdict"] == "KILL"
    assert r["nets"][config.SETTLEMENT_F_PASS]["before"] < 0


def test_verdict_kill_by_tails(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_MIN_HEDGED_N", 1)
    monkeypatch.setattr(config, "SETTLEMENT_P_VOID", 0.0)
    monkeypatch.setattr(config, "SETTLEMENT_P_DIV", 0.7)   # tails1 = 0.7·(10·1.0) = 7 > before(0.7)=6.7
    r = sb._evaluate([_rec(1.0, flat=0.1) for _ in range(10)])
    assert r["verdict"] == "KILL"
    fp = config.SETTLEMENT_F_PASS
    assert r["nets"][fp]["before"] > 0 and r["nets"][fp]["n1"] <= 0   # killed only by tails


def test_verdict_fill_race(small_n):
    # pnl=flat=0.5: net(0.3)=1.5−3.5=−2 <0, net(0.7)=3.5−1.5=+2 >0 → fill-race.
    r = sb._evaluate([_rec(0.5, flat=0.5) for _ in range(10)])
    assert r["verdict"] == "KILL: fill-race artifact"


def test_verdict_balanced_on_tails(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_MIN_HEDGED_N", 1)
    monkeypatch.setattr(config, "SETTLEMENT_P_VOID", 0.0)
    monkeypatch.setattr(config, "SETTLEMENT_P_DIV", 0.2)   # tails1=2.0, tails2=4.0; before(0.3)=2.3
    r = sb._evaluate([_rec(1.0, flat=0.1) for _ in range(10)])
    assert r["verdict"] == "PROVISIONAL PASS (BALANCED ON TAIL ASSUMPTION)"
    fp = config.SETTLEMENT_F_PASS
    assert r["nets"][fp]["n1"] > 0 and r["nets"][fp]["n2"] <= 0


def test_breakeven_computed(monkeypatch):
    # Non-zero rates so both tails actually move the net (P_VOID=0 → W_VOID has no
    # effect → break-even W_VOID is correctly None).
    monkeypatch.setattr(config, "SETTLEMENT_MIN_HEDGED_N", 1)
    monkeypatch.setattr(config, "SETTLEMENT_P_VOID", 0.005)
    monkeypatch.setattr(config, "SETTLEMENT_P_DIV", 0.002)
    H = [_rec(1.0, flat=0.1) for _ in range(10)]
    assert sb._breakeven(H, "P_DIV") is not None
    assert sb._breakeven(H, "W_VOID") is not None


# ── _poly_depth_suspect: frozen-feed detector ───────────────────────────────────

def _smpl(offset, poly_ask, poly_depth, poly_bid, kalshi_ask):
    return dict(wf_id="1", offset_s=str(offset), poly_ask=poly_ask, poly_depth=poly_depth,
                poly_bid=poly_bid, kalshi_ask=kalshi_ask, kalshi_fillable="9151")


def _smpl_r(offset, poly_ask, poly_depth, poly_bid, kalshi_ask, rest_poly_depth):
    """_smpl + the rest_poly_depth column. rest_poly_depth=None OMITS the key entirely
    (a blank / not-measured sample), so blank≠zero can be exercised."""
    d = _smpl(offset, poly_ask, poly_depth, poly_bid, kalshi_ask)
    if rest_poly_depth is not None:
        d["rest_poly_depth"] = rest_poly_depth
    return d


def _total_freeze_samples():
    # The Mexico case: Poly ask/bid/depth byte-identical; Kalshi marching 0.36→0.31.
    return [
        _smpl(0.0, "0.3600", "726257", "0.3500", "0.3600"),
        _smpl(1.0, "0.3600", "726257", "0.3500", "0.3200"),
        _smpl(2.0, "0.3600", "726257", "0.3500", "0.3100"),
        _smpl(3.0, "0.3600", "726257", "0.3500", "0.3200"),
    ]


def test_detector_total_freeze_flagged():
    assert sb._poly_depth_suspect(_total_freeze_samples()) == (True, "total-freeze")


def test_detector_partial_freeze_flagged():
    # Poly ASK ticks 0.36→0.37→0.38 but depth stays byte-identical → phantom depth.
    # This is the case exact-equality-across-all-offsets would MISS; the price-flow proxy
    # catches it. (Caught because Poly's own price moved while depth didn't.)
    s = [
        _smpl(0.0, "0.3600", "726257", "0.3500", "0.3600"),
        _smpl(1.0, "0.3700", "726257", "0.3600", "0.3600"),
        _smpl(2.0, "0.3800", "726257", "0.3700", "0.3600"),
    ]
    assert sb._poly_depth_suspect(s) == (True, "partial-freeze")


def test_detector_live_row_retained():
    # Depth decrements as price moves through it → a real, live book. Not suspect.
    s = [
        _smpl(0.0, "0.3600", "1000", "0.3500", "0.3600"),
        _smpl(1.0, "0.3700", "700", "0.3600", "0.3500"),
        _smpl(2.0, "0.3800", "300", "0.3700", "0.3400"),
    ]
    assert sb._poly_depth_suspect(s) == (False, None)


def test_detector_quiet_book_not_flagged():
    # Nothing moved at all → with only these fields, a freeze is indistinguishable from a
    # genuinely quiet book. Conservatively NOT flagged (don't fabricate exclusions).
    s = [_smpl(0.0, "0.3600", "1000", "0.3500", "0.3600"),
         _smpl(1.0, "0.3600", "1000", "0.3500", "0.3600")]
    assert sb._poly_depth_suspect(s) == (False, None)


def test_detector_insufficient_samples_not_flagged():
    assert sb._poly_depth_suspect([]) == (False, None)
    assert sb._poly_depth_suspect([_smpl(0.0, "0.36", "1000", "0.35", "0.36")]) == (False, None)


# ── _poly_depth_suspect: WS-vs-REST primary signal (rest_poly_depth present) ─────

def test_detector_ws_flat_rest_moved_flagged():
    # WS depth byte-identical while the fresh REST book MOVED → definitive freeze, caught
    # with NO price tick (exactly the case the price-movement heuristic MISSES).
    s = [_smpl_r(0.0, "0.3600", "726257", "0.3500", "0.3600", "1000"),
         _smpl_r(1.0, "0.3600", "726257", "0.3500", "0.3600", "700"),
         _smpl_r(2.0, "0.3600", "726257", "0.3500", "0.3600", "300")]
    assert sb._poly_depth_suspect(s) == (True, "ws-frozen-rest-moved")


def test_detector_static_phantom_rest_disagrees_flagged():
    # WS flat at 726257, REST flat at 3 → the static phantom; magnitudes disagree >10×.
    s = [_smpl_r(0.0, "0.3600", "726257", "0.3500", "0.3600", "3"),
         _smpl_r(1.0, "0.3600", "726257", "0.3500", "0.3600", "3")]
    assert sb._poly_depth_suspect(s) == (True, "ws-rest-divergence")


def test_detector_de_false_positive_rest_agrees():
    # WS depth flat AND REST flat & equal, but poly_ask TICKS. Old code flagged this
    # "partial-freeze" (false positive); REST confirms a genuinely stable book → NOT suspect.
    s = [_smpl_r(0.0, "0.3600", "1000", "0.3500", "0.3600", "1000"),
         _smpl_r(1.0, "0.3700", "1000", "0.3600", "0.3600", "1000"),
         _smpl_r(2.0, "0.3800", "1000", "0.3700", "0.3600", "1000")]
    assert sb._poly_depth_suspect(s) == (False, None)


def test_detector_rest_confirms_live():
    # WS depth varies (live book consuming flow), REST agrees → not suspect.
    s = [_smpl_r(0.0, "0.3600", "1000", "0.3500", "0.3600", "1000"),
         _smpl_r(1.0, "0.3700", "700", "0.3600", "0.3500", "700"),
         _smpl_r(2.0, "0.3800", "300", "0.3700", "0.3400", "300")]
    assert sb._poly_depth_suspect(s) == (False, None)


def test_detector_tolerance_within_jitter():
    # WS flat, REST flat at a slightly different value (1.05×, well under 10×) → agreement.
    s = [_smpl_r(0.0, "0.3600", "1000", "0.3500", "0.3600", "1050"),
         _smpl_r(1.0, "0.3600", "1000", "0.3500", "0.3600", "1050")]
    assert sb._poly_depth_suspect(s) == (False, None)


def test_detector_near_zero_no_false_phantom():
    # Both readings ≤ the 50-share floor → any gap is thin-book noise, not a phantom.
    s = [_smpl_r(0.0, "0.3600", "3", "0.3500", "0.3600", "5"),
         _smpl_r(1.0, "0.3600", "3", "0.3500", "0.3600", "5")]
    assert sb._poly_depth_suspect(s) == (False, None)


def test_detector_freeze_at_zero_caught():
    # WS depth flat at 0 while REST shows a real, moving book → freeze-at-zero. The fallback's
    # max<=0 guard would miss this; the REST path catches it.
    s = [_smpl_r(0.0, "0.3600", "0", "0.3500", "0.3600", "200"),
         _smpl_r(1.0, "0.3600", "0", "0.3500", "0.3600", "150"),
         _smpl_r(2.0, "0.3600", "0", "0.3500", "0.3600", "90")]
    assert sb._poly_depth_suspect(s) == (True, "ws-frozen-rest-moved")


def test_detector_partial_rest_one_sample_falls_back():
    # Only ONE row carries REST → len(rest)=1 < 2 → fallback price heuristic (Kalshi moved).
    s = [_smpl_r(0.0, "0.3600", "726257", "0.3500", "0.3600", "300"),
         _smpl_r(1.0, "0.3600", "726257", "0.3500", "0.3200", None),
         _smpl_r(2.0, "0.3600", "726257", "0.3500", "0.3100", None)]
    assert sb._poly_depth_suspect(s) == (True, "total-freeze")


def test_detector_partial_rest_two_samples_uses_rest():
    # TWO rows carry REST that varies → len(rest)=2 → REST path engages (WS flat, REST moved).
    s = [_smpl_r(0.0, "0.3600", "726257", "0.3500", "0.3600", "100"),
         _smpl_r(1.0, "0.3600", "726257", "0.3500", "0.3600", "500"),
         _smpl_r(2.0, "0.3600", "726257", "0.3500", "0.3600", None)]
    assert sb._poly_depth_suspect(s) == (True, "ws-frozen-rest-moved")


def test_detector_blank_rest_not_treated_as_zero():
    # WS flat 726257; REST = [blank, 726257, 726257] → len(rest)=2, REST flat & AGREES → NOT
    # suspect. A blank-as-0 bug would pair 726257 vs 0 → wrongly emit ws-rest-divergence.
    s = [_smpl_r(0.0, "0.3600", "726257", "0.3500", "0.3600", None),
         _smpl_r(1.0, "0.3600", "726257", "0.3500", "0.3600", "726257"),
         _smpl_r(2.0, "0.3600", "726257", "0.3500", "0.3600", "726257")]
    assert sb._poly_depth_suspect(s) == (False, None)


# ── exclusion from H + the floor interaction ────────────────────────────────────

def _mex_wf():
    # The known Mexico phantom: a high logged fillable (726257) that the SAMPLES freeze
    # detector flags → excluded from H regardless of how hedgeable it looks.
    return dict(wf_id="1", shares="13", poly_fillable="726257", kalshi_fillable="9151",
                poly_limit="0.4035", kalshi_limit="0.5600",
                poly_ask_raw="0.3600", kalshi_ask="0.3600")


def test_mexico_phantom_built_then_excluded(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_W_VOID", 0.10)
    rec = sb._build_record(_mex_wf(), _total_freeze_samples())
    assert rec["poly_depth_suspect"] is True
    assert rec["suspect_reason"] == "total-freeze"
    assert rec["naive_hedged"] is True          # phantom depth makes it LOOK hedgeable
    H, excluded = sb._partition([rec])
    assert H == []                               # ...but it never reaches the trusted set
    assert [r["wf_id"] for r in excluded] == ["1"]


def test_exclusions_caused_insufficient_logic(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_MIN_HEDGED_N", 1)
    # H=2 → 0.3·2=0.6 < 1 (insufficient); H+excl=4 → 0.3·4=1.2 ≥ 1 → exclusions CAUSED it.
    assert sb._exclusions_caused_insufficient(2, 2) is True
    # Even adding the 1 excluded back, 0.3·2=0.6 < 1 → would be insufficient regardless.
    assert sb._exclusions_caused_insufficient(1, 1) is False
    # And the surviving clean H really does evaluate to INSUFFICIENT DATA.
    assert sb._evaluate([_rec(1.0), _rec(1.0)])["verdict"] == "INSUFFICIENT DATA"


# ── census: clustering of suspects in the high-pnl tail ──────────────────────────

def _crec(pnl, suspect, reason=None, wf="x", s=1):
    return {"wf_id": wf, "s": s, "poly_fillable": 1000.0, "hedged_pnl": pnl,
            "poly_depth_suspect": suspect, "suspect_reason": reason, "naive_hedged": True}


def test_census_flags_high_pnl_clustering():
    recs = [_crec(5.0, True, "total-freeze"), _crec(4.0, True, "total-freeze"),
            _crec(0.1, False), _crec(0.2, False)]
    c = sb._census(recs)
    assert c["n_suspect"] == 2
    assert c["by_reason"] == {"total-freeze": 2}
    assert c["clustered_high_pnl"] is True       # suspects ARE the fat-win rows → warn

def test_census_no_clustering_when_suspects_are_low_pnl():
    recs = [_crec(0.1, True, "total-freeze"), _crec(5.0, False), _crec(4.0, False)]
    c = sb._census(recs)
    assert c["clustered_high_pnl"] is False       # suspect mean < clean mean → no warning
