"""
Sweeper Bot V2 - WebSocket Client (P1)

P1: Real-time order book updates via WebSocket with auto-reconnection.
- Connects to Polymarket CLOB WS endpoints (market + user channels)
- Auto-reconnect with exponential backoff (1s -> 2s -> 4s -> 8s -> 16s, max 30s)
- Heartbeat/ping to detect stale connections
- Graceful shutdown via stop() method
- Callback-based message handling

Usage:
    ws = MarketWSClient(on_message=handler, on_status=status_handler)
    ws.start()  # non-blocking, runs in background thread
    ws.stop()   # graceful shutdown
"""
import json
import logging
import threading
import time

logger = logging.getLogger("sweeper.ws")

try:
    import websocket  # websocket-client library
    _HAS_WS = True
except ImportError:
    _HAS_WS = False
    logger.warning("websocket-client not installed - WS features disabled (pip install websocket-client)")


class BaseWSClient:
    """Base WebSocket client with reconnection logic."""

    def __init__(self, url, on_message=None, on_status=None, on_connect=None):
        self.url = url
        self.on_message = on_message
        self.on_status = on_status
        self.on_connect = on_connect
        self._ws = None
        self._thread = None
        self._running = False
        self._backoff = 1.0
        self._max_backoff = 30.0
        self._last_ping = 0.0
        self._ping_interval = 15.0  # seconds

    def start(self):
        """Start the WebSocket client in a background thread."""
        if not _HAS_WS:
            logger.warning("Cannot start WS - websocket-client not installed")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-client")
        self._thread.start()
        logger.info(f"WS client started for {self.url}")

    def stop(self):
        """Gracefully stop the WebSocket client."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WS client stopped")

    def _run(self):
        """Main loop with reconnection logic."""
        while self._running:
            try:
                if self.on_status:
                    self.on_status("connecting")
                self._ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_msg,
                    on_error=self._on_err,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=self._ping_interval, ping_timeout=10)
            except Exception as e:
                logger.error(f"WS connection error: {e}")

            if not self._running:
                break

            # Exponential backoff before reconnecting
            logger.info(f"WS reconnecting in {self._backoff:.1f}s...")
            if self.on_status:
                self.on_status(f"reconnecting in {self._backoff:.1f}s")
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, self._max_backoff)

    def _on_open(self, ws):
        logger.info(f"WS connected to {self.url}")
        self._backoff = 1.0  # reset backoff on successful connect
        if self.on_status:
            self.on_status("connected")
        if self.on_connect:
            self.on_connect(ws)

    def _on_msg(self, ws, message):
        try:
            data = json.loads(message)
            if self.on_message:
                self.on_message(data)
        except json.JSONDecodeError:
            logger.debug(f"WS non-JSON message: {message[:100]}")
        except Exception as e:
            logger.error(f"WS message handler error: {e}")

    def _on_err(self, ws, error):
        logger.error(f"WS error: {error}")
        if self.on_status:
            self.on_status(f"error: {error}")

    def _on_close(self, ws, close_status, close_msg):
        logger.warning(f"WS closed: {close_status} {close_msg}")
        if self.on_status:
            self.on_status("disconnected")

    def send(self, data: dict):
        """Send a JSON message to the WebSocket."""
        if self._ws:
            try:
                self._ws.send(json.dumps(data))
            except Exception as e:
                logger.error(f"WS send failed: {e}")


class MarketWSClient(BaseWSClient):
    """Market channel WebSocket - order book updates."""

    def __init__(self, on_message=None, on_status=None, on_connect=None, url=None):
        from config.settings import WS_MARKET
        super().__init__(url or WS_MARKET, on_message, on_status, on_connect)

    def subscribe(self, asset_ids: list):
        """Subscribe to market channels for given asset IDs."""
        msg = {"type": "Market", "assets_ids": asset_ids}
        self.send(msg)
        logger.info(f"WS subscribed to {len(asset_ids)} assets")


class UserWSClient(BaseWSClient):
    """User channel WebSocket - order fills, trades, balance updates."""

    def __init__(self, on_message=None, on_status=None, on_connect=None, url=None, auth_params=None):
        from config.settings import WS_USER
        super().__init__(url or WS_USER, on_message, on_status, on_connect)
        self.auth_params = auth_params or {}

    def _on_open(self, ws):
        super()._on_open(ws)
        # Send auth message if needed
        if self.auth_params:
            self.send({"type": "User", **self.auth_params})
            logger.info("WS user channel authenticated")