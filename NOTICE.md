# Notice — public snapshot vs. private operation

This repository is a **public engineering snapshot** of a personal research project. It is
complete enough to read, test, and evaluate as a system, but it is intentionally **not a
turnkey trading deployment**. The separation below is deliberate, not an oversight.

## What lives here (public)

- The full engine architecture: concurrent WebSocket feeds, matching, detection, the
  fresh-read verify, execution/unwind/strand handling, reconciliation, settlement scoring.
- The fee models and the reasoning behind every gate, threshold, and safety invariant.
- The 800+ test suite, including the mutation-tested money path and venue-behavior pins.
- The case study and architecture write-ups — the methodology is the point.

## What is kept private (not in this tree)

- **Credentials and deployment config** — venue API keys, wallet keys, and the production
  `.env`. Nothing here authenticates to any account.
- **Tuned production values** — position sizing, loss caps, and calibrated thresholds are
  supplied at runtime via `.env`. The numeric defaults in `bot/core/config.py` are
  illustrative and, where they encode calibration, deliberately conservative.
- **Operational market data** — the settlement-equivalence allowlist
  (`bot/kalshi/matcher.py`) and the MLB team table (`bot/core/matcher.py`) ship as
  publicly-verifiable illustrative subsets. Any additional or experimental pairs, and the
  full league table, are merged at runtime from optional gitignored overlays
  (`bot/kalshi/_private_pairs.py`, `bot/core/_private_teams.py`) when present. The macro
  (Fed/CPI) pairs follow the same pattern and ship empty (`MACRO_PAIRS = []`).
- **Measurement results** — the accumulating dry-run data on which detected edges are
  fillable and settle clean lives in `logs/` (gitignored) and private notes, not here.

## Why

Cross-venue arbitrage is capacity- and competition-constrained: a validated, fillable edge
degrades as more participants race for the same fills. Publishing the engineering and the
measurement discipline is the intent; publishing a runnable edge is not. The code here
demonstrates how the system is built and how it reasons — the parts that would let someone
skip the measurement and deploy against live capital are held back on purpose.
