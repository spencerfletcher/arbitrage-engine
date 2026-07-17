"""
bot/core/trade_logger.py
────────────────────────
The executed-trade JSONL logger for the LIVE Kalshi↔Poly US cross-arb.

Relocated verbatim from the legacy (now DORMANT) bot/polymarket/arb_logs.py so the live fire
path no longer imports from that dormant package. Writes one JSON line per executed hedge to
logs/trades.log — the permanent record that safety.py (daily-loss cap) and settlement_scorer.py
(realized post-settlement P&L) read by path. The destination path is UNCHANGED by the move.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discord_webhook import DiscordWebhook, DiscordEmbed

from bot.core import config

_TRADE_LOG_PATH = Path("logs/trades.log")


def _append_jsonl(path: Path, record: dict) -> None:
    """Append a record (with auto-added ISO timestamp) as one JSON line."""
    record["timestamp"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def log_trade(record: dict[str, Any]) -> None:
    """Append a structured trade record to trades.log and post to Discord."""
    _append_jsonl(_TRADE_LOG_PATH, record)

    # Never post to the live channel from under pytest, even with a real .env —
    # mirrors the guard in logger.py so a test can't fire a real trade alert.
    if "pytest" in sys.modules:
        return

    if config.DISCORD_WEBHOOK_URL and (not config.DRY_RUN or config.DISCORD_NOTIFY_DRY_RUN):
        try:
            # Timeout + off-loop: this runs from _record_hedge, i.e. mid-execution with
            # _execution_lock held. See alerts._dispatch.
            from bot.core.alerts import _DISCORD_TIMEOUT_S, _dispatch
            webhook = DiscordWebhook(url=config.DISCORD_WEBHOOK_URL,
                                     timeout=_DISCORD_TIMEOUT_S)
            color = "00ff00" if record.get("status") == "EXECUTED" else "808080"
            embed = DiscordEmbed(
                title=f"✓ Trade Executed: {record.get('event', 'Unknown')}",
                color=color,
            )
            embed.set_footer(text=f"Total Cost: ${record.get('cost', 0):.2f} | Expected Profit: +${record.get('expected_profit', 0):.2f}")

            embed.add_embed_field(name="Type", value=str(record.get("type")), inline=True)
            embed.add_embed_field(name="Leg A", value=str(record.get("leg_a")), inline=True)
            embed.add_embed_field(name="Leg B", value=str(record.get("leg_b")), inline=True)

            webhook.add_embed(embed)
            _dispatch(webhook)
        except Exception:
            pass
