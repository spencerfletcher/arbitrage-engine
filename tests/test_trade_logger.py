"""trade_logger.log_trade — relocated from bot/polymarket/arb_logs.py. Pin the destination
(safety.py + settlement_scorer read logs/trades.log by path) and the JSONL write."""
import json
from pathlib import Path

import bot.core.trade_logger as tl


def test_destination_is_trades_log_unchanged():
    # The move must NOT change where executed trades are recorded — safety.py (daily-loss cap)
    # and settlement_scorer.py (realized P&L) both read this exact path.
    assert tl._TRADE_LOG_PATH == Path("logs/trades.log")


def test_log_trade_writes_one_jsonl_record(tmp_path, monkeypatch):
    dest = tmp_path / "trades.log"
    monkeypatch.setattr(tl, "_TRADE_LOG_PATH", dest)
    tl.log_trade({"event": "A vs B", "status": "EXECUTED", "cost": 1.0, "expected_profit": 0.10})
    lines = dest.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "A vs B" and rec["status"] == "EXECUTED"
    assert "timestamp" in rec                       # ISO timestamp auto-added on write
    # Discord is skipped under pytest (the `"pytest" in sys.modules` guard) — the write still lands.


def test_live_import_path():
    # The live fire path must import log_trade from core, not the dormant polymarket package.
    import bot.runner.kalshi_arb as ka
    assert ka.log_trade is tl.log_trade
