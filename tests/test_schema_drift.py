"""schema_drift_alerts — the venue-schema-rename detector. Fires only when BOTH venues have a
live slate (Kalshi markets + ≥min_poly_events Poly game EVENTS) yet 0 matched cross-pairs.

The 2026-06-24 fix gates on Poly raw EVENTS (not pairs) to kill the nightly between-slates false
positive. Tests are organized around the operational-state case list. Pure logic; counts in, alerts out."""
from bot.runner.common import schema_drift_alerts

SERIES = ["KXMLBGAME", "KXWCGAME"]


def _run(kalshi, poly_events, poly_pairs, matched, cycles, alerted, n=1):
    """Drive n consecutive discovery cycles with the same counts; return the LAST cycle's
    (to_alert, recovered). cycles/alerted carry state across calls like the live loop."""
    out = ([], [])
    for _ in range(n):
        out = schema_drift_alerts(SERIES, kalshi, poly_events, poly_pairs, matched, cycles, alerted)
    return out


# ── CASE 1: real parse/rename break (Poly HAS a slate of events, builds 0 pairs) → MUST FIRE ──
def test_case1_real_break_fires():
    # The whole point: Poly fetched 15 MLB game events, a rename made all build 0 pairs, Kalshi
    # lists the games → after the debounce, fire. (Pins the alert's core job — can't be disabled.)
    cycles, alerted = {}, set()
    kalshi = {"KXMLBGAME": 15}
    poly_events, poly_pairs, matched = {"KXMLBGAME": 15}, {"KXMLBGAME": 0}, {}
    to_alert, _ = schema_drift_alerts(SERIES, kalshi, poly_events, poly_pairs, matched, cycles, alerted)
    assert to_alert == []                                # cycle 1: under debounce
    to_alert, _ = schema_drift_alerts(SERIES, kalshi, poly_events, poly_pairs, matched, cycles, alerted)
    assert to_alert == [("KXMLBGAME", 15, 15, 0)]        # cycle 2: FIRES (series, kalshi, events, pairs)
    assert alerted == {"KXMLBGAME"}


def test_case1_real_break_even_if_a_pair_built():
    # A rename can leave a few events parseable: events=15, pairs=2, but still 0 MATCHED → fire.
    cycles, alerted = {}, set()
    to_alert, _ = _run({"KXMLBGAME": 15}, {"KXMLBGAME": 15}, {"KXMLBGAME": 2}, {}, cycles, alerted, n=2)
    assert to_alert == [("KXMLBGAME", 15, 15, 2)]


# ── CASE 2: between-slates / no games (Poly 0 events, Kalshi still lists scheduled) → SILENT (the bug) ──
def test_case2_between_slates_silent():
    # THE regression test for the 2026-06-24 fix: overnight Poly slate empty (0 events), Kalshi
    # still lists 80 scheduled MLB markets. Old code fired every night; new code stays silent.
    cycles, alerted = {}, set()
    to_alert, _ = _run({"KXMLBGAME": 80}, {"KXMLBGAME": 0}, {}, {}, cycles, alerted, n=4)
    assert to_alert == [] and alerted == set()


# ── CASE 3: off-season (no games either venue) → SILENT ──
def test_case3_offseason_silent():
    cycles, alerted = {}, set()
    to_alert, recovered = _run({}, {}, {}, {}, cycles, alerted, n=3)
    assert to_alert == [] and recovered == [] and alerted == set()


# ── CASE 4: single stranded unpaired market (1 Poly event, no Kalshi counterpart) → SILENT ──
def test_case4_stranded_single_market_silent():
    # The all-day WC market that never has a Kalshi pair: events=1 (< min_poly_events=3), pairs=1,
    # 0 matched. NOT a break — a market with no counterpart. Must NOT fire (else it alarms forever).
    cycles, alerted = {}, set()
    to_alert, _ = _run({"KXWCGAME": 72}, {"KXWCGAME": 1}, {"KXWCGAME": 1}, {}, cycles, alerted, n=4)
    assert to_alert == [] and alerted == set()


def test_case4_threshold_boundary():
    # Pins min_poly_events=2: 1 unpaired event → silent (single stranded); 2 → fires (slate break).
    cycles, alerted = {}, set()
    to_alert, _ = _run({"KXMLBGAME": 9}, {"KXMLBGAME": 1}, {"KXMLBGAME": 0}, {}, cycles, alerted, n=3)
    assert to_alert == []                                # 1 < 2 → single stranded, silent
    cycles, alerted = {}, set()
    to_alert, _ = _run({"KXMLBGAME": 9}, {"KXMLBGAME": 2}, {"KXMLBGAME": 0}, {}, cycles, alerted, n=2)
    assert to_alert == [("KXMLBGAME", 9, 2, 0)]          # 2 == floor → fires


# ── CASE 5: market-open race (Kalshi lists a game before Poly discovery) → debounce absorbs it ──
def test_case5_market_open_race_debounced():
    cycles, alerted = {}, set()
    # one transient broken cycle, then it heals (Poly discovery catches up → pairs match)
    to_alert, _ = schema_drift_alerts(SERIES, {"KXMLBGAME": 15}, {"KXMLBGAME": 15}, {"KXMLBGAME": 0}, {},
                                      cycles, alerted)
    assert to_alert == []                                # 1 cycle < debounce → no alert
    to_alert, recovered = schema_drift_alerts(SERIES, {"KXMLBGAME": 15}, {"KXMLBGAME": 15},
                                              {"KXMLBGAME": 15}, {"KXMLBGAME": 15}, cycles, alerted)
    assert to_alert == [] and recovered == []            # healed before the debounce → never fired


# ── healthy + recovery/re-arm ──
def test_healthy_silent():
    cycles, alerted = {}, set()
    to_alert, _ = _run({"KXMLBGAME": 9}, {"KXMLBGAME": 9}, {"KXMLBGAME": 9}, {"KXMLBGAME": 9},
                       cycles, alerted, n=3)
    assert to_alert == [] and alerted == set()


def test_recovery_rearms():
    cycles, alerted = {}, set()
    broken = ({"KXMLBGAME": 9}, {"KXMLBGAME": 9}, {"KXMLBGAME": 0}, {})
    _run(*broken, cycles, alerted, n=2)
    assert alerted == {"KXMLBGAME"}                       # fired
    to_alert, recovered = schema_drift_alerts(           # matching recovers
        SERIES, {"KXMLBGAME": 9}, {"KXMLBGAME": 9}, {"KXMLBGAME": 9}, {"KXMLBGAME": 9}, cycles, alerted)
    assert recovered == ["KXMLBGAME"] and alerted == set()
    to_alert, _ = _run(*broken, cycles, alerted, n=2)    # breaks again → re-alerts
    assert to_alert == [("KXMLBGAME", 9, 9, 0)]
