# Architecture

A technical map of the cross-venue arbitrage engine. For *why* it's built this way — the failure
modes it defends against and the measurements behind the design — see [`CASE_STUDY.md`](CASE_STUDY.md).

## Venues

The live engine trades two prediction-market exchanges against each other:

- **Kalshi** (`bot/kalshi/`) — REST + a WebSocket ticker channel.
- **Polymarket US** (`bot/poly_us/`) — REST + a raw WebSocket order-book feed.

Venue selection is a config flag (`POLY_VENUE=us`). The `bot/polymarket/` package is **vestigial** —
see [Dormant code](#dormant-code) below.

## Module layout

```
bot/
  core/        shared infra: config, matching, position tracker, alerts, safety caps, ws timing
  kalshi/      Kalshi REST/WS client, cross-arb detection + fee model, settlement equivalence
  poly_us/     Polymarket US client + WebSocket order-book feed + side parsing
  runner/      the orchestrator: execution/fire path, gates, unwind + strand handling,
               venue reconciliation, settlement scoring, balance tracking
  polymarket/  DORMANT v1 venue plumbing (not on the live path)
tests/         pure, mocked unit tests — run on every change
scripts/       read-only measurement + calibration probes
```

`BotRunner` (`bot/runner/runner.py`) is assembled from mixins — one per concern (Poly discovery,
Kalshi cross-arb, macro side-path, balance, reconciliation) — so each subsystem is isolated and
independently testable.

## The live pipeline

Event-driven. A price change on either venue's WebSocket feed triggers detection; everything from
detection to firing is designed to complete well inside the lifetime of a typical edge.

1. **Discover** (polled, ~5 min) — fetch open markets for the covered sports; prime the feed's
   subscription set.
2. **Match** — pair a Kalshi market with a Polymarket market for the *same* real-world event. This
   is **fail-closed**: an unrecognized sport, or a pairing that can't be positively verified as the
   same game (by settlement basis and start-time consistency), matches *nothing*.
3. **Detect** — on each tick, compute the cross-venue edge **net of both venues' taker fees**
   (`bot/kalshi/cross_arb.py`). A positive edge means buying both sides costs less than the \$1.00 the
   winning side pays.
4. **Persist** — require the edge to survive a short confirmation window before acting; a one-tick
   flicker is not tradeable.
5. **Verify** — immediately before firing, re-read *both* real order books fresh (cache-busted, to
   defeat a 30-second CDN cache) and confirm the size is actually there at the intended price. Most
   detected edges die here — the verify is the primary defense against a lagging quote that never had
   liquidity behind it.
6. **Fire** — place the first leg, then hedge the second **sized to the first leg's actual fill**,
   never the detection-time estimate. Both legs use immediate-or-cancel semantics.
7. **Unwind / halt** — if the hedge misses, flatten the first leg. A flatten that can't complete is a
   *stranded* leg: it trips a global pause (no new trades), a loud alert, and a control loop that
   keeps trying to unwind.
8. **Settle** — at event resolution, both venues pay \$1 on the winning outcome and the spread is
   realized.

## Safety invariants

These are load-bearing and hold regardless of configuration:

- **Fail toward doing nothing.** Every uncertain read (an unreadable book, an unmapped sport, an
  unverified settlement pairing) resolves to the option that costs an *opportunity*, never the one
  that costs *money*.
- **Size off actual fills.** The hedge leg can never be larger than what the first leg actually
  filled, so a partial fill can't create a naked over-hedge.
- **Strand detection → global pause.** Any leg left unhedged halts all trading until it's resolved.
- **Persistent exposure caps** and a **cumulative realized-loss cap** bound total risk.
- **Position reconciliation.** A loop periodically asks each venue what it actually holds and
  compares against the engine's belief — the local tracker is never trusted as the sole source of
  truth. An unrecognized venue response resolves to "cannot verify," never a convenient "flat."
- **Settlement equivalence.** Two markets are hedged only after a rules-text check that they resolve
  on the same official basis (winner, draw handling, overtime). The allowlist is fail-closed.

## Dormant code

`bot/polymarket/` is the project's **v1**: it began as a same-platform Polymarket + sportsbook
arbitrage bot, then evolved into the cross-venue engine described above. The v1 detection and
execution logic has been removed from this snapshot; what remains (`client.py`, `feed.py`) is only
the venue-selection fallback the runner constructs in non-`us` mode. It does not run on the live path,
and the global Polymarket CLOB is geoblocked from a US server regardless.

## Testing

The suite is pure and mocked (no network), runs in a few seconds, and is treated as a gate. Two
disciplines make it trustworthy: the money path is **mutation-tested** (reverting a safety fix must
fail a specific test), and venue behavior is pinned against **captured real API responses** rather
than hand-written fixtures — because a fixture written from the same belief as the code can only ever
confirm it.

## Status

Runs in a dry-run measurement mode; no live arbitrage trade has been placed. The only live orders
have been tiny probes to ground-truth venue behavior (fees, order latency). See the README's status
note and the case study.
