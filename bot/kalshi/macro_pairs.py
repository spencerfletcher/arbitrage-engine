"""Macro cross-platform arb allowlist (Polymarket ↔ Kalshi) + startup validation.

This module owns the manually curated, exact-ID allowlist of macro event-market
pairs (Fed rate decisions, CPI prints). Each MacroPair asserts, by hand, that a
specific Polymarket token and a specific Kalshi side resolve complementarily on
the same real-world event. Nothing on either platform can prove that assertion —
the mandatory `comment` is the sole audit trail. See the design spec:
docs/superpowers/specs/2026-06-15-macro-cross-platform-arb-design.md
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import requests

from bot.core import config
from bot.core.logger import get_logger
from bot.kalshi.ladder import classify_market

log = get_logger(__name__)


@dataclass(frozen=True)
class MacroPair:
    # --- identity (exact-match) ---
    poly_condition_id: str   # Polymarket market condition ID
    poly_token: str          # exact clobTokenId to BUY on Polymarket
    kalshi_ticker: str       # exact Kalshi market ticker
    kalshi_side: str         # "yes" or "no" — the side to BUY on Kalshi
    # --- semantics ---
    comment: str             # MANDATORY plain-English: what each leg pays
    category: str            # "fed" | "cpi" — drives the Poly fee rate


# Polymarket fee rate by macro category. Sports use per-token fetched rates on
# the existing path; macro categories carry a flat per-category rate.
_MACRO_FEE_RATES: dict[str, float] = {"fed": 0.04, "cpi": 0.04}


# The curated allowlist. Empty until pairs are hand-added by the operator.
MACRO_PAIRS: list[MacroPair] = []


def _resolve_poly_tokens(condition_id: str) -> list[str] | None:
    """Return the clobTokenIds for a live Polymarket market, or None if the
    condition ID does not resolve or the lookup fails.

    Uses Gamma `GET /markets?condition_ids=<id>`. clobTokenIds comes back as a
    JSON-encoded string list (same shape bot/scanner.py parses).
    """
    try:
        resp = requests.get(
            f"{config.GAMMA_HOST}/markets",
            params={"condition_ids": condition_id},
            timeout=15,
        )
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:  # network / JSON / HTTP error — treat as unresolved
        log.warning(f"Macro validation: Poly lookup failed for {condition_id}: {e}")
        return None

    if not markets:
        return None
    raw = markets[0].get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or not raw:
        return None
    return [str(t) for t in raw]


def validate_macro_pairs(
    pairs: list[MacroPair],
    live_kalshi_markets: dict[str, dict],
) -> list[MacroPair]:
    """Validate each MacroPair against the live markets; return the pairs that pass.

    `live_kalshi_markets` maps Kalshi ticker -> raw market dict (must include
    `status`, `floor_strike`, `cap_strike`). A pair failing ANY check is skipped
    (not returned) and logged at WARNING with the reason and offending IDs. One
    bad entry never blocks the rest. Zero valid pairs is not fatal.
    """
    valid: list[MacroPair] = []
    for p in pairs:
        # 1. Schema check.
        if p.kalshi_side not in ("yes", "no"):
            log.warning(f"Macro pair skipped (bad kalshi_side={p.kalshi_side!r}): "
                        f"{p.kalshi_ticker} / {p.poly_condition_id}")
            continue
        if p.category not in _MACRO_FEE_RATES:
            log.warning(f"Macro pair skipped (bad category={p.category!r}): "
                        f"{p.kalshi_ticker} / {p.poly_condition_id}")
            continue
        if not (p.poly_condition_id.strip() and p.poly_token.strip()
                and p.kalshi_ticker.strip()):
            log.warning(f"Macro pair skipped (empty identity field): "
                        f"{p.kalshi_ticker!r} / {p.poly_condition_id!r}")
            continue
        if not p.comment.strip():
            log.warning(f"Macro pair skipped (empty comment): "
                        f"{p.kalshi_ticker} / {p.poly_condition_id}")
            continue

        # 2. Kalshi existence + structure (binary, open). Reject ladder/bucket.
        km = live_kalshi_markets.get(p.kalshi_ticker)
        if km is None:
            log.warning(f"Macro pair skipped (Kalshi ticker not in live markets): "
                        f"{p.kalshi_ticker}")
            continue
        if km.get("status") not in ("open", "active"):
            log.warning(f"Macro pair skipped (Kalshi market not open, "
                        f"status={km.get('status')!r}): {p.kalshi_ticker}")
            continue
        if classify_market(km) == "bucket":
            log.warning(f"Macro pair skipped (Kalshi market is a range bucket — "
                        f"not binary): {p.kalshi_ticker}")
            continue

        # 3. Polymarket existence + token membership.
        tokens = _resolve_poly_tokens(p.poly_condition_id)
        if tokens is None:
            log.warning(f"Macro pair skipped (Poly condition ID did not resolve): "
                        f"{p.poly_condition_id}")
            continue
        if p.poly_token not in tokens:
            log.warning(f"Macro pair skipped (poly_token not in market's "
                        f"clobTokenIds): {p.poly_token} / {p.poly_condition_id}")
            continue

        valid.append(p)

    return valid
