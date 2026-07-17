"""Tests for scripts/settlement_capture.py — the capture-forward audit + its hard
guard against writing the live loss-cap file.

The headline test is assert_safe_output: capture-forward records HYPOTHETICAL
settlements, so routing one into logs/execution_pnl.csv would feed a fake loss into
safety.cumulative_realized_loss. That must be a hard assertion, not a convention.
"""
import os

import pytest

import scripts.settlement_capture as sc


# ── assert_safe_output: the inverted blind-to-losses guard ────────────────────

@pytest.mark.parametrize("bad", [
    "logs/execution_pnl.csv",
    "./logs/execution_pnl.csv",
    "execution_pnl.csv",
    "logs/../logs/execution_pnl.csv",
])
def test_capture_cannot_write_execution_pnl(bad):
    with pytest.raises(ValueError):
        sc.assert_safe_output(bad)


def test_capture_allows_its_own_output():
    # The real output path must pass cleanly.
    sc.assert_safe_output(sc._OUT)
    sc.assert_safe_output("logs/settlement_capture.csv")


# ── load_candidates: the three-source union (fill_success added as gap-filler) ─

def _write_csv(path, header, rows):
    import csv as _csv
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _point_sources(monkeypatch, tmp_path, *, wf=None, rej=None, fs=None):
    """Repoint the three module-level candidate-source paths at tmp files (or absent)."""
    wf_p = str(tmp_path / "would_fire.csv")
    rej_p = str(tmp_path / "rejected_edges.csv")
    fs_p = str(tmp_path / "fill_success.csv")
    if wf is not None:
        _write_csv(wf_p, ["event", "poly_token", "kalshi_ticker", "kalshi_side",
                          "poly_ask_raw", "kalshi_ask", "edge", "shares"], wf)
    if rej is not None:
        _write_csv(rej_p, ["event", "poly_token", "kalshi_ticker", "kalshi_side",
                           "poly_ask", "kalshi_ask", "edge", "shares"], rej)
    if fs is not None:
        _write_csv(fs_p, ["event", "poly_token", "kalshi_ticker", "kalshi_side", "outcome",
                          "live_poly_ask", "kalshi_ask", "edge", "target_shares"], fs)
    monkeypatch.setattr(sc, "_WF", wf_p if wf is not None else str(tmp_path / "absent_wf.csv"))
    monkeypatch.setattr(sc, "_REJ", rej_p if rej is not None else str(tmp_path / "absent_rej.csv"))
    monkeypatch.setattr(sc, "_FS", fs_p if fs is not None else str(tmp_path / "absent_fs.csv"))


def test_fill_success_only_both_fill_is_covered(monkeypatch, tmp_path):
    # The gap case: a both_fill present ONLY in fill_success (absent from wf/rejected) must still
    # become a candidate, keyed correctly, with fill-time metadata mapped from live_poly_ask/etc.
    _point_sources(monkeypatch, tmp_path, fs=[
        ["Game A", "tokA", "KXMLBGAME-A-X", "yes", "both_fill", "0.42", "0.55", "0.03", "12"],
        ["Game A", "tokB", "KXMLBGAME-A-Y", "no", "poly_moved", "0.50", "0.50", "0.02", "8"],
    ])
    cands = sc.load_candidates()
    keys = {(c["poly_token"], c["kalshi_ticker"], c["kalshi_side"]) for c in cands}
    assert ("tokA", "KXMLBGAME-A-X", "yes") in keys          # the both_fill is covered
    assert ("tokB", "KXMLBGAME-A-Y", "no") not in keys       # the poly_moved row is NOT (hedges only)
    bf = next(c for c in cands if c["poly_token"] == "tokA")
    assert bf["source"] == "fill_success"
    assert bf["poly_ask"] == 0.42 and bf["kalshi_ask"] == 0.55   # fill-time ask basis, _num-parsed
    assert bf["shares"] == 12.0


def test_union_dedupe_first_source_wins(monkeypatch, tmp_path):
    # A triple in BOTH rejected and fill_success → ONE candidate (dedupe is across the union, not
    # per-source), and the higher-priority rejected row wins on the shared key.
    _point_sources(
        monkeypatch, tmp_path,
        rej=[["Game A", "tokA", "KXMLBGAME-A-X", "yes", "0.40", "0.55", "0.05", "10"]],
        fs=[["Game A", "tokA", "KXMLBGAME-A-X", "yes", "both_fill", "0.42", "0.55", "0.03", "12"]],
    )
    cands = sc.load_candidates()
    shared = [c for c in cands if (c["poly_token"], c["kalshi_ticker"], c["kalshi_side"])
              == ("tokA", "KXMLBGAME-A-X", "yes")]
    assert len(shared) == 1                       # exactly one candidate, not two
    assert shared[0]["source"] == "rejected"      # rejected (higher priority) wins the shared key
    assert shared[0]["poly_ask"] == 0.40          # → its detection-time ask, not fill_success's


def test_would_fire_outranks_fill_success(monkeypatch, tmp_path):
    # would_fire is the richest source; a both_fill also in would_fire keeps the would_fire row.
    _point_sources(
        monkeypatch, tmp_path,
        wf=[["Game A", "tokA", "KXMLBGAME-A-X", "yes", "0.41", "0.55", "0.04", "9"]],
        fs=[["Game A", "tokA", "KXMLBGAME-A-X", "yes", "both_fill", "0.42", "0.55", "0.03", "12"]],
    )
    cands = sc.load_candidates()
    shared = [c for c in cands if c["poly_token"] == "tokA"]
    assert len(shared) == 1 and shared[0]["source"] == "would_fire"


def test_both_fill_in_all_three_sources_never_sourced_fill_success(monkeypatch, tmp_path):
    # CONSEQUENCE pin (T1-B): the prior tests pin the per-source PRIORITY (wf>fs, rej>fs); this pins
    # the production CONSEQUENCE the priority causes — that source=='fill_success' is structurally
    # UNREACHABLE. In production every both_fill triple also churns as a detection-time would_fire/
    # rejected edge, so the fill_success branch always loses the dedupe key. A future reader filtering
    # settlement_capture by source=='fill_success' to isolate filled hedges would silently get ZERO
    # rows — this test exists so that trap is caught, not just the priority mechanism.
    _point_sources(
        monkeypatch, tmp_path,
        wf=[["Game A", "tokA", "KXMLBGAME-A-X", "yes", "0.41", "0.55", "0.04", "9"]],
        rej=[["Game A", "tokA", "KXMLBGAME-A-X", "yes", "0.40", "0.55", "0.05", "10"]],
        fs=[["Game A", "tokA", "KXMLBGAME-A-X", "yes", "both_fill", "0.42", "0.55", "0.03", "12"]],
    )
    cands = sc.load_candidates()
    shared = [c for c in cands if c["poly_token"] == "tokA"]
    assert len(shared) == 1
    assert shared[0]["source"] == "would_fire"            # highest priority wins the shared key
    assert shared[0]["source"] != "fill_success"          # the structural consequence, pinned
    # and across the whole output, NO covered triple is ever sourced fill_success
    assert all(c["source"] != "fill_success" for c in cands)


# ── series_of ─────────────────────────────────────────────────────────────────

def test_series_of_strips_to_prefix():
    assert sc.series_of("KXMLBGAME-26JUN181410CLEMIL-CLE") == "KXMLBGAME"
    assert sc.series_of("KXWCGAME-26JUN18MEXKOR-MEX") == "KXWCGAME"
    assert sc.series_of("") == ""


# ── audit: per-series game counts + void/divergence + conflict detection ───────

def _rec(ticker, outcome):
    return {"kalshi_ticker": ticker, "outcome": outcome}


def test_audit_counts_distinct_games_per_series():
    records = [
        _rec("KXMLBGAME-A-X", "clean"),
        _rec("KXMLBGAME-B-Y", "void"),
        _rec("KXMLBGAME-C-Z", "clean"),
        _rec("KXWCGAME-D-W", "clean"),
    ]
    a = sc.audit(records)
    mlb = a["by_series"]["KXMLBGAME"]
    assert mlb["games"] == 3 and mlb["settled"] == 3
    assert mlb["clean"] == 2 and mlb["void"] == 1 and mlb["diverge"] == 0
    assert mlb["p_void"] == pytest.approx(1 / 3)
    wc = a["by_series"]["KXWCGAME"]
    assert wc["games"] == 1 and wc["clean"] == 1


def test_audit_divergence_rate():
    records = [
        _rec("KXMLBGAME-A-X", "clean"),
        _rec("KXMLBGAME-B-Y", "divergence"),
    ]
    a = sc.audit(records)["by_series"]["KXMLBGAME"]
    assert a["diverge"] == 1
    assert a["p_div"] == pytest.approx(0.5)


def test_audit_pending_excluded_from_rates():
    records = [
        _rec("KXMLBGAME-A-X", "clean"),
        _rec("KXMLBGAME-B-Y", "pending"),
    ]
    a = sc.audit(records)["by_series"]["KXMLBGAME"]
    assert a["games"] == 2 and a["settled"] == 1 and a["pending"] == 1
    assert a["p_void"] == pytest.approx(0.0)   # over the 1 settled game


def test_audit_flags_direction_disagreement():
    # Same game scored two ways with different definite outcomes → a real bug to surface.
    records = [
        _rec("KXMLBGAME-A-X", "clean"),
        _rec("KXMLBGAME-A-X", "divergence"),
    ]
    a = sc.audit(records)
    assert a["conflicts"], "conflicting outcomes for one game must be flagged"


def test_audit_dedups_same_game_same_outcome():
    records = [
        _rec("KXMLBGAME-A-X", "clean"),
        _rec("KXMLBGAME-A-X", "clean"),   # same game, second direction, agrees
    ]
    a = sc.audit(records)["by_series"]["KXMLBGAME"]
    assert a["games"] == 1 and not sc.audit(records)["conflicts"]


# ── economic-event dedup (two settlement-equivalent tickers = one hedge) ───────

def _full(event, poly_token, ticker, p_pay, k_pay, outcome, pnl):
    return {"event": event, "poly_token": poly_token, "kalshi_ticker": ticker,
            "poly_payout": p_pay, "kalshi_payout": k_pay, "outcome": outcome,
            "realized_pnl": pnl}


def test_economic_event_dedup_collapses_equivalent_tickers():
    # wf1/2: one hedge on the TOR/CHC game logged on two settlement-equivalent Kalshi
    # tickers (CHC yes ≡ TOR no), identical payouts → ONE economic event. The opposite
    # direction uses the ::short complement poly_token, so it's a genuinely distinct hedge.
    ev, tok = "Toronto Blue Jays vs. Chicago Cubs", "aec-mlb-tor-chc-2026-06-20"
    records = [
        _full(ev, tok, "KXMLBGAME-26JUN201420TORCHC-CHC", "1.0000", "0.0000", "clean", "0.1837"),
        _full(ev, tok, "KXMLBGAME-26JUN201420TORCHC-TOR", "1.0000", "0.0000", "clean", "0.1238"),
        _full(ev, tok + "::short", "KXMLBGAME-26JUN201420TORCHC-TOR", "0.0000", "1.0000", "clean", "0.0500"),
    ]
    n_rows, n_events, pnl, n_settled = sc._economic_events(records)
    assert n_rows == 3 and n_events == 2 and n_settled == 2
    # one representative per event: first equivalent row (0.1837) + the ::short row (0.0500),
    # NOT 0.1837 + 0.1238 + 0.0500 (which would double-count the single TOR/CHC hedge).
    assert pnl == pytest.approx(0.1837 + 0.0500)


def test_economic_event_dedup_keeps_payout_mismatch_separate():
    # Same (event, poly_token, game_id) but DIFFERENT payouts must NOT merge — a real
    # divergence can never be silently collapsed away (payouts are in the key on purpose).
    ev, tok = "X vs Y", "aec-mlb-x-y-2026-06-20"
    records = [
        _full(ev, tok, "KXMLBGAME-DATEXY-X", "1.0000", "0.0000", "clean", "0.10"),
        _full(ev, tok, "KXMLBGAME-DATEXY-Y", "0.0000", "1.0000", "divergence", "0.20"),
    ]
    _, n_events, pnl, n_settled = sc._economic_events(records)
    assert n_events == 2 and n_settled == 2 and pnl == pytest.approx(0.30)


def test_economic_event_dedup_pending_excluded_from_pnl():
    # Two equivalent PENDING rows collapse to one event but contribute no P&L.
    ev, tok = "P vs Q", "aec-mlb-p-q-2026-06-20"
    records = [
        _full(ev, tok, "KXMLBGAME-DATEPQ-P", "", "", "pending", ""),
        _full(ev, tok, "KXMLBGAME-DATEPQ-Q", "", "", "pending", ""),
    ]
    n_rows, n_events, pnl, n_settled = sc._economic_events(records)
    assert n_rows == 2 and n_events == 1 and n_settled == 0 and pnl == pytest.approx(0.0)


# ── atomic write: a killed/failed rewrite never corrupts the verdict CSV ───────
# These pin the PROPERTY the timer depends on (money-path tooling): _OUT changes ONLY via a
# completed os.replace of a fully-written sibling .tmp, so an hourly run killed mid-write
# (reboot, OOM, systemctl stop) is a clean no-op-this-hour — never a partial/truncated CSV.

_SEED_ROW = {
    "timestamp": "2026-06-20T00:00:00Z", "source": "rejected", "event": "Game A",
    "series": "KXMLBGAME", "poly_token": "tokA", "kalshi_ticker": "KXMLBGAME-A-X",
    "kalshi_side": "yes", "outcome": "clean",
}


def _setup_no_fetch(monkeypatch, tmp_path):
    """Seed _OUT with one SETTLED row and point a source at the SAME triple, so `todo` is empty
    → main() reaches the write path with NO network/client. The real write runs in isolation."""
    import csv as _csv
    out = str(tmp_path / "settlement_capture.csv")
    with open(out, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=sc._HEADER)
        w.writeheader()
        w.writerow({c: _SEED_ROW.get(c, "") for c in sc._HEADER})
    monkeypatch.setattr(sc, "_OUT", out)
    _point_sources(monkeypatch, tmp_path,
                   rej=[["Game A", "tokA", "KXMLBGAME-A-X", "yes", "0.40", "0.55", "0.05", "10"]])
    return out


def _read_rows(path):
    import csv as _csv
    with open(path, newline="") as f:
        return list(_csv.DictReader(f))


async def test_atomic_write_failed_rename_leaves_original_intact(monkeypatch, tmp_path):
    """The invariant, at the worst moment: if the rewrite dies AT the rename boundary, the prior
    complete CSV must survive whole — a killed run never leaves a partial verdict denominator."""
    out = _setup_no_fetch(monkeypatch, tmp_path)

    def _boom(src, dst):
        raise RuntimeError("simulated kill at the rename boundary")
    monkeypatch.setattr(sc.os, "replace", _boom)

    with pytest.raises(RuntimeError):
        await sc.main()

    rows = _read_rows(out)                                   # _OUT untouched, fully parseable
    assert len(rows) == 1
    assert rows[0]["poly_token"] == "tokA" and rows[0]["outcome"] == "clean"
    # the partial work landed on .tmp, never on the real target (a stale .tmp is harmless —
    # the next run overwrites it — but it is NOT _OUT)
    assert out.endswith("settlement_capture.csv")


async def test_atomic_write_success_replaces_and_leaves_no_tmp(monkeypatch, tmp_path):
    """The success path: a completed run rewrites _OUT whole via os.replace and leaves no .tmp."""
    out = _setup_no_fetch(monkeypatch, tmp_path)

    await sc.main()

    rows = _read_rows(out)
    assert len(rows) == 1 and rows[0]["outcome"] == "clean"   # settled row preserved (idempotent)
    assert not os.path.exists(out + ".tmp")                   # os.replace consumed the temp
