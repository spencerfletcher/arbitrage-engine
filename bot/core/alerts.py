"""
bot/alerts.py
─────────────
Runtime push notifications: Discord embeds + Pushover phone alerts.

  - send_kalshi_arb_alert  — Kalshi↔Poly arb outcome (filled / stranded / missed)
  - send_strand_pause_alert — bot paused on a stranded leg, awaiting decision
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests as _requests
from discord_webhook import DiscordWebhook, DiscordEmbed

from bot.core import config

log = logging.getLogger(__name__)

# ⚠️ DiscordWebhook defaults its timeout to None, and requests reads None as "wait forever" — no
# connect timeout, no read timeout. Every webhook here MUST pass this. A single un-timed-out post
# is enough to hang the process indefinitely: proven 2026-07-16 against a black-hole socket (a
# task ticking 19x/0.2s emitted one warning and never ticked again, still blocked at 25s), and the
# systemd unit has Restart=always but no WatchdogSec, so a blocked-but-alive bot reports
# `active (running)` forever. 10s is generous for a real slow post and bounded for a hung one.
_DISCORD_TIMEOUT_S = 10


def _dispatch(webhook: DiscordWebhook) -> None:
    """Send `webhook` WITHOUT blocking the event loop.

    `webhook.execute()` is synchronous `requests`. Called straight from a coroutine it stalls the
    single event loop — both WS feeds stop reading, the `websockets` library cannot answer server
    pings (same loop) so the sockets drop, and every timer freezes with them. That is bad enough
    while idle; four of these fire from the exec helpers while `_execution_lock` is held, i.e.
    potentially between a Poly fill and the Kalshi hedge, on a path whose whole budget is ~100ms.
    The alarm becoming the outage is not hypothetical here: the loudest callers are the strand and
    reconcile alerts, which fire exactly when a leg is stranded or unknown exposure exists.

    Off the loop, a hung post costs an idle worker for `_DISCORD_TIMEOUT_S` and nothing else. The
    result is deliberately not awaited — an alert is best-effort and must never be able to fail,
    delay, or raise into a decision path. With no loop running (a `to_thread` worker, a script, a
    test) we are already off the loop, so post inline.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            webhook.execute()
        except Exception as e:
            log.debug(f"discord post failed (non-fatal): {e!r}")
        return
    loop.run_in_executor(None, _execute_quietly, webhook)


def _execute_quietly(webhook: DiscordWebhook) -> None:
    """Post and swallow. Runs on an executor thread; a raise there would be an unretrieved-future
    warning at best, and must never reach a caller that is mid-hedge."""
    try:
        webhook.execute()
    except Exception as e:
        log.debug(f"discord post failed (non-fatal): {e!r}")


def send_kalshi_arb_alert(
    opp: Any,
    poly_filled: bool,
    kalshi_filled: bool,
    is_dry_run: bool = False,
) -> None:
    """Discord + Pushover alert for a Kalshi↔Poly arb execution outcome.

    Three states:
      both legs filled → ✅ FILLED (success)
      exactly one leg  → 🚨 STRANDED (urgent, @everyone — needs eyes)
      neither leg      → ⚪ MISSED (quiet heads-up; no money moved)
    Callers should throttle the MISSED case to avoid spam on a re-firing match.
    """
    if not config.DISCORD_WEBHOOK_URL:
        return
    if is_dry_run and not config.DISCORD_NOTIFY_DRY_RUN:
        return

    n_filled = int(poly_filled) + int(kalshi_filled)
    mode = "🔍 DRY RUN" if is_dry_run else "⚡ LIVE"
    poly_cost = opp.poly_ask * opp.shares
    poly_status = "✅ FILLED" if poly_filled else "❌ NOT FILLED"
    kalshi_status = "✅ FILLED" if kalshi_filled else "❌ NOT FILLED"

    if n_filled == 2:
        heading, color, mention, footer, prio = "✅ ARB FILLED", "00ff00", "", None, 1
    elif n_filled == 1:
        heading, color, mention, footer, prio = (
            "🚨 STRANDED LEG", "ff0000",
            "@everyone" if not is_dry_run else "",
            "⚠️ One leg stranded — bot auto-unwinds; verify position", 1,
        )
    else:
        heading, color, mention, footer, prio = (
            "⚪ ARB MISSED (no fill)", "999999", "",
            "Both FOKs killed — no money moved", -1,
        )

    try:
        webhook = DiscordWebhook(url=config.DISCORD_WEBHOOK_URL, content=mention,
                                 timeout=_DISCORD_TIMEOUT_S)
        embed = DiscordEmbed(
            title=f"{heading}: {opp.event_title} [{mode}]",
            description=(
                f"**Edge: {opp.edge * 100:.2f}%  |  "
                f"Profit: ${opp.guaranteed_profit:.2f}  |  "
                f"Cost: ${opp.total_cost:.2f}**"
            ),
            color=color,
        )
        embed.add_embed_field(
            name="🟢 Polymarket US Leg",
            value=(
                f"{poly_status}\nBUY **{opp.poly_team}** YES\n"
                f"Price: {opp.poly_ask:.3f}  |  Shares: {opp.shares}  |  "
                f"Cost: ${poly_cost:.2f}"
            ),
            inline=False,
        )
        embed.add_embed_field(
            name="🔵 Kalshi Leg",
            value=(
                f"{kalshi_status}\nBUY **{opp.kalshi_team}** {opp.kalshi_side.upper()}\n"
                f"Price: {opp.kalshi_ask:.3f}  |  Contracts: {opp.shares}  |  "
                f"Ticker: {opp.kalshi_ticker}"
            ),
            inline=False,
        )
        if footer:
            embed.set_footer(text=footer)
        embed.set_timestamp()
        webhook.add_embed(embed)
        _dispatch(webhook)
    except Exception:
        pass

    _send_pushover(
        title=f"{heading}: {opp.event_title}",
        message=(
            f"Edge: {opp.edge * 100:.2f}% | Profit: ${opp.guaranteed_profit:.2f}\n"
            f"Poly {opp.poly_team} YES @ {opp.poly_ask:.3f}: {poly_status}\n"
            f"Kalshi {opp.kalshi_team} {opp.kalshi_side} @ {opp.kalshi_ask:.3f}: {kalshi_status}"
        ),
        priority=prio if not is_dry_run else -1,
    )


def send_proper_edge_alert(
    opp: Any,
    poly_depth: float,
    kalshi_fillable: float,
    is_dry_run: bool = False,
) -> None:
    """Discord-only alert for a 'proper edge' — an opportunity that passed EVERY gate
    (settlement-equivalent, persistent, book-trusted, fresh, exit-liquid, real entry
    depth on Kalshi, fresh Poly price). Posts to DISCORD_EDGE_WEBHOOK_URL (its own
    channel) so the edge feed stays clean; falls back to the main webhook if unset.

    Unlike fill/strand alerts this fires in DRY too — the channel's purpose is to
    surface real, fillable edges as they happen without digging through logs."""
    url = config.DISCORD_EDGE_WEBHOOK_URL or config.DISCORD_WEBHOOK_URL
    if not url:
        return
    mode = "🔍 DRY" if is_dry_run else "⚡ LIVE"
    try:
        webhook = DiscordWebhook(url=url, timeout=_DISCORD_TIMEOUT_S)
        embed = DiscordEmbed(
            title=f"📈 EDGE: {opp.event_title} [{mode}]",
            description=(
                f"**Edge {opp.edge * 100:.2f}%  |  Profit ${opp.guaranteed_profit:.2f}  |  "
                f"{opp.shares} shares**"
            ),
            color="2ecc71",
        )
        embed.add_embed_field(
            name="🟢 Polymarket US",
            value=(
                f"BUY **{opp.poly_team}** @ {opp.poly_ask:.3f}\n"
                f"depth {poly_depth:.0f}  |  `{opp.poly_token}`"
            ),
            inline=False,
        )
        embed.add_embed_field(
            name="🔵 Kalshi",
            value=(
                f"BUY **{opp.kalshi_team}** {opp.kalshi_side.upper()} @ {opp.kalshi_ask:.3f}\n"
                f"fillable {kalshi_fillable:.0f}  |  `{opp.kalshi_ticker}`"
            ),
            inline=False,
        )
        embed.set_timestamp()
        webhook.add_embed(embed)
        _dispatch(webhook)
    except Exception:
        pass


def send_strand_pause_alert(details: str, resume_path: str) -> None:
    """Loud alert that the bot is PAUSED on a stranded leg, awaiting a decision.

    Two choices: clear & resume (`touch resume_path`) or stop & examine (Ctrl-C).
    Sent once per strand event; high priority so it's not missed.
    """
    msg = (
        f"🚨 BOT PAUSED — stranded leg, all trading halted.\n{details}\n\n"
        f"CLEAR & RESUME:  touch {resume_path}\n"
        f"STOP & EXAMINE:  Ctrl-C the bot"
    )
    if config.DISCORD_WEBHOOK_URL and (not config.DRY_RUN or config.DISCORD_NOTIFY_DRY_RUN):
        try:
            webhook = DiscordWebhook(
                url=config.DISCORD_WEBHOOK_URL,
                content="@everyone" if not config.DRY_RUN else "",
                timeout=_DISCORD_TIMEOUT_S,
            )
            embed = DiscordEmbed(title="🚨 BOT PAUSED — Stranded Leg",
                                 description=msg, color="ff0000")
            embed.set_timestamp()
            webhook.add_embed(embed)
            _dispatch(webhook)
        except Exception:
            pass
    _send_pushover(title="🚨 BOT PAUSED — stranded leg", message=msg,
                   priority=1 if not config.DRY_RUN else -1)


def send_schema_drift_alert(series: str, kalshi_n: int, poly_events: int, poly_pairs: int) -> None:
    """Warn that a sport has a live slate on BOTH venues (Kalshi markets + Poly game events) yet
    0 matched cross-pairs — the silent venue-schema-rename signal (2026-06-23: Poly renamed MLB's
    sportsMarketType and matching was blind ~5.5h). Sent once per episode (caller debounces + re-arms).

    Fires in DRY too — unlike trade alerts, this is a measurement-integrity warning: a silent
    matching-zero corrupts the data being accumulated for the profit verdict, not just live P&L.
    """
    msg = (
        f"⚠️ Schema drift: {series} has {kalshi_n} live Kalshi markets and {poly_events} live Poly "
        f"events but 0 matched cross-pairs (Poly pairs={poly_pairs}). Likely a venue schema rename "
        f"(sportsMarketType / series ticker) or matcher break — cross-arb matching is BLIND for this sport."
    )
    if config.DISCORD_WEBHOOK_URL:
        try:
            webhook = DiscordWebhook(url=config.DISCORD_WEBHOOK_URL, timeout=_DISCORD_TIMEOUT_S)
            embed = DiscordEmbed(title="⚠️ Schema drift — matching blind",
                                 description=msg, color="ff8800")
            embed.set_timestamp()
            webhook.add_embed(embed)
            _dispatch(webhook)
        except Exception:
            pass
    _send_pushover(title="⚠️ Schema drift — matching blind", message=msg, priority=0)


def _send_pushover(title: str, message: str, priority: int = 0) -> None:
    """Send a push notification via Pushover. Silent no-op if not configured."""
    if not config.PUSHOVER_USER_KEY or not config.PUSHOVER_API_TOKEN:
        return
    try:
        _requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": config.PUSHOVER_API_TOKEN,
                "user": config.PUSHOVER_USER_KEY,
                "title": title,
                "message": message,
                "priority": priority,
                "sound": "cashregister" if priority >= 1 else "pushover",
            },
            timeout=5,
        )
    except Exception:
        pass


def send_reconcile_alert(msg: str) -> None:
    """Venue-vs-tracker reconciliation alert: unknown exposure, or a failure to VERIFY it.

    Distinct from send_strand_pause_alert (which _strand_control_loop already fires once a
    divergence is recorded) for two reasons: it carries the operator's ACKNOWLEDGE option, and it
    is the ONLY alert for CANNOT-VERIFY — which strands nothing, so nothing else would speak.

    Fires even in DRY: a real venue position while we believe we are flat is exactly as wrong in
    DRY as in live (in DRY the bot places no orders at all, so it can only be unrecorded).
    """
    if config.DISCORD_WEBHOOK_URL and (not config.DRY_RUN or config.DISCORD_NOTIFY_DRY_RUN):
        try:
            webhook = DiscordWebhook(
                url=config.DISCORD_WEBHOOK_URL,
                content="@everyone" if not config.DRY_RUN else "",
                timeout=_DISCORD_TIMEOUT_S,
            )
            embed = DiscordEmbed(title="🚨 POSITION RECONCILIATION",
                                 description=msg, color="ff0000")
            embed.set_timestamp()
            webhook.add_embed(embed)
            _dispatch(webhook)
        except Exception:
            pass
    _send_pushover(title="🚨 Position reconciliation", message=msg,
                   priority=1 if not config.DRY_RUN else -1)
