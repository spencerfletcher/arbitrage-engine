"""_max_id_col0 lets per-run counters (wf_id, arb_id/EDGE #N) resume across restarts."""
from bot.runner.runner import _max_id_col0


def test_missing_file_returns_zero(tmp_path):
    assert _max_id_col0(str(tmp_path / "nope.csv")) == 0


def test_empty_file_returns_zero(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("")
    assert _max_id_col0(str(p)) == 0


def test_reads_max_id_skipping_header_and_bad_rows(tmp_path):
    p = tmp_path / "edges.csv"
    # header, then rows whose col 0 is the id; include an out-of-order max and a junk row.
    p.write_text(
        "arb_id,timestamp,event\n"
        "1,t,A\n"
        "7,t,B\n"          # max is not the last row
        "3,t,C\n"
        ",t,blank-id\n"    # blank col 0 → skipped, not a crash
        "x,t,non-int\n"    # non-int col 0 → skipped
    )
    assert _max_id_col0(str(p)) == 7


def test_multi_path_returns_global_max(tmp_path):
    # wf_id is seeded from BOTH would_fire.csv and would_fire_samples.csv — the global max
    # across them must win so the backtest join never collides after an independent rotation.
    wf = tmp_path / "would_fire.csv"
    wfs = tmp_path / "would_fire_samples.csv"
    wf.write_text("wf_id,timestamp\n3,t\n4,t\n")          # would_fire truncated to a low max
    wfs.write_text("wf_id,offset_s\n4,0\n9,1\n9,2\n")     # samples retain a higher wf_id
    assert _max_id_col0(str(wf), str(wfs)) == 9


def test_multi_path_skips_missing(tmp_path):
    wf = tmp_path / "would_fire.csv"
    wf.write_text("wf_id,timestamp\n5,t\n")
    missing = tmp_path / "nope.csv"
    assert _max_id_col0(str(wf), str(missing)) == 5
