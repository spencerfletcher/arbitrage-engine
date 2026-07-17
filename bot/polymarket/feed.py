"""
bot/feed.py
───────────
Maintains a real-time hot cache of order book prices via the 
CLOB WebSocket API. This allows the bot to check arbitrage conditions 
instantly against memory without rate-limited REST polling.
"""
import asyncio
import json
import websockets
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from bot.core import config
from bot.core.logger import get_logger

log = get_logger(__name__)

import time

@dataclass
class TokenPriceData:
    best_bid: float = 0.0
    best_ask: float = 1.0
    ask_depth: float = 0.0   # contracts available at best ask (from WS book snapshot)
    last_updated: float = field(default_factory=time.time)

class OrderBookCache:
    """
    Maintains the hot in-memory state of tokens via WebSocket updates.
    """
    def __init__(self):
        self._prices: Dict[str, TokenPriceData] = {}
        self._ws_uri = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self._active_tokens: set[str] = set()
        self._running = False
        self._ws = None
        self._on_update_callback = None
        self.subscriptions_ready = False
        
    def set_callback(self, callback) -> None:
        """Set a synchronous callable to be triggered on significant price ticks."""
        self._on_update_callback = callback
        
    def get_price(self, token_id: str) -> Optional[TokenPriceData]:
        return self._prices.get(token_id)
        
    def has_all_prices(self, tokens: List[str]) -> bool:
        """Returns True if the cache has a valid quote for all specified tokens."""
        for t in tokens:
            if t not in self._prices:
                return False
            # If the ask is exactly 1.0 (default empty book state), consider it missing
            if self._prices[t].best_ask >= 1.0:
                return False
        return True

    def update_subscriptions(self, token_ids: List[str]) -> None:
        """Thread-safe way for the main loop to tell the feed which tokens to track."""
        new_tokens = set(token_ids)
        to_add = list(new_tokens - self._active_tokens)
        to_remove = list(self._active_tokens - new_tokens)
        
        self._active_tokens = new_tokens
        
        # If we have an active WebSocket connection, send the diffs directly
        if getattr(self, '_ws', None) and self._ws.state.name == "OPEN":
            if to_add:
                asyncio.create_task(self._send_subscription_chunks(to_add, "subscribe"))
                log.info(f"➕ Added {len(to_add)} new tokens to live subscription.")
            if to_remove:
                asyncio.create_task(self._send_subscription_chunks(to_remove, "unsubscribe"))
                log.info(f"➖ Removed {len(to_remove)} dead tokens from live subscription.")

    async def _send_subscription_chunks(self, tokens: List[str], operation: str) -> None:
        """Send a massive operation in small JSON chunks to respect API limits."""
        if not getattr(self, '_ws', None) or self._ws.state.name != "OPEN":
            return
            
        chunk_size = 100
        for i in range(0, len(tokens), chunk_size):
            chunk = tokens[i : i + chunk_size]
            payload = {
                "assets_ids": chunk,
                "type": "market",
                "operation": operation
            }
            try:
                await self._ws.send(json.dumps(payload))
                await asyncio.sleep(0.5)
            except Exception as e:
                log.debug(f"Error sending {operation} chunk: {e}")
                break

    async def _handle_message(self, message: str) -> None:
        """Parses incoming WS ticks and updates the hot cache."""
        try:
            data = json.loads(message)
            
            # Polymarket WS can send lists or dicts depending on the event type
            messages = data if isinstance(data, list) else [data]
            updated = False
            
            for tick in messages:
                event_type = tick.get("event_type") or tick.get("event")
                
                if event_type == "price_change":
                    changes = tick.get("price_changes", [])
                    for change in changes:
                        token_id = change.get("asset_id")
                        if not token_id:
                            continue
                            
                        # Extract the new best prices from this specific tick
                        # If null or missing, we keep the previous value or default
                        raw_ask = change.get("best_ask")
                        raw_bid = change.get("best_bid")
                        
                        if raw_ask is None and raw_bid is None:
                            continue

                        if token_id not in self._prices:
                            self._prices[token_id] = TokenPriceData()
                            
                        p = self._prices[token_id]
                        if raw_ask is not None:
                            p.best_ask = float(raw_ask)
                        if raw_bid is not None:
                            p.best_bid = float(raw_bid)
                        
                        p.last_updated = time.time()
                        updated = True

                elif event_type == "book":
                    # Full order book snapshot
                    token_id = tick.get("asset_id")
                    if not token_id:
                        continue
                        
                    asks = tick.get("asks", [])
                    bids = tick.get("bids", [])
                    
                    if token_id not in self._prices:
                        self._prices[token_id] = TokenPriceData()
                        
                    p = self._prices[token_id]
                    
                    # Polymarket returns levels worst-first (asks high→low, bids
                    # low→high), so the true best is min(asks)/max(bids), not index 0.
                    if asks and len(asks) > 0:
                        best_ask_price = min(float(a.get("price", 1.0)) for a in asks)
                        p.best_ask = best_ask_price
                        p.ask_depth = sum(
                            float(a.get("size", 0.0)) for a in asks
                            if float(a.get("price", 1.0)) == best_ask_price
                        )
                    if bids and len(bids) > 0:
                        p.best_bid = max(float(b.get("price", 0.0)) for b in bids)
                        
                    p.last_updated = time.time()
                    updated = True
            
            # Trigger the downstream arb detector instantly if cache materially updated
            if updated and self._on_update_callback:
                self._on_update_callback()
                
        except json.JSONDecodeError:
            pass
        except Exception as e:
            log.error(f"Error parsing WS message: {e}")

    async def run_forever(self) -> None:
        """Main async loop that connects to the WS and listens."""
        self._running = True
        
        while self._running:
            self.subscriptions_ready = False
            self._prices.clear()
            try:
                log.info(f"🔌 Connecting to CLOB WebSocket: {self._ws_uri}")
                async with websockets.connect(self._ws_uri, ping_interval=20, ping_timeout=20, max_size=2**24) as ws:
                    self._ws = ws
                    log.info("✅ WebSocket connected.")
                    
                    # Initial subscription if we already know what to track
                    current_subs = list(self._active_tokens)
                    if current_subs:
                        async def sub_task():
                            await self._send_subscription_chunks(current_subs, "subscribe")
                            log.info(f"✅ Subscribed to {len(current_subs)} tokens in chunks.")
                            self.subscriptions_ready = True
                            
                        # Run the heavy 24-second chunking burst in the background
                        # This allows the client to instantly enter the `ws.recv()` loop
                        # and answer server-side PINGs, preventing the 30s idle disconnect.
                        asyncio.create_task(sub_task())
                    else:
                        self.subscriptions_ready = True
                    
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                            await self._handle_message(msg)
                        except asyncio.TimeoutError:
                            # Standard for quiet markets. Do not disconnect!
                            # The ping/pong via the websockets library keeps it alive.
                            pass
                            
            except asyncio.TimeoutError:
                self.subscriptions_ready = False
                # This only triggers if the initial connection fails completely
                log.warning("WebSocket initial connection timeout. Reconnecting...")
            except websockets.exceptions.ConnectionClosed as e:
                self.subscriptions_ready = False
                log.warning(f"WebSocket closed randomly: {e.code}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                self.subscriptions_ready = False
                log.error(f"WebSocket critical error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
