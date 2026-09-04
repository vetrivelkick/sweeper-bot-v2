"""
Sweeper Bot V2 - WebSocket Client (P1)

P1: Real-time order book updates via WebSocket with auto-reconnection.
- Connects to Polymarket CLOB WS endpoints (market + user channels)
- Auto-reconnect with exponential backoff (1s -> 2s -> 4s -> 8s -> 16s, max 30s)
- Heartbeat/ping to detect stale connections
- Graceful shutdown via stop() method
- Callback-based message handling
- AUDIT FIX #15: Reconnection metrics, max attempts, status reporting

SECTION 7 AUDIT: WebSocket connection handling
- Subscription recovery after reconnection (re-subscribe on reconnect)
- Backoff jitter to prevent thundering herd
- Sustained connection threshold (only reset backoff if connection > 30s)
- Connection timeout for initial connection
- Thread safety with lock for shared state
- Message type validation
- Health check with stale connection detection
- Unsubscribe method
- Fallback flag for REST polling degradation
- WS URL validation
"""
import json
import logging
import threading
import time
import random

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
        # SECTION 7 AUDIT: Validate URL
        if not url or not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
            raise ValueError(f"Invalid WebSocket URL: {url}")
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
        self._ping_interval = 15.0
        self._max_reconnects = 10
        self._reconnect_count = 0
        self._total_reconnects = 0
        self._last_disconnect_time = None
        self._connected_since = None
        self._is_connected = False
        # SECTION 7 AUDIT: Thread safety
        self._lock = threading.Lock()
        # SECTION 7 AUDIT: Sustained connection threshold
        self._sustained_threshold = 30.0
        self._connection_duration = 0.0
        # SECTION 7 AUDIT: Connection timeout
        self._connect_timeout = 10.0
        # SECTION 7 AUDIT: Health check
        self._last_message_time = 0.0
        self._stale_threshold = 120.0
        # SECTION 7 AUDIT: Fallback flag
        self._fallback_active = False
        # SECTION 7 AUDIT: Subscriptions for recovery
        self._subscriptions = []

    def start(self):
        if not _HAS_WS:
            logger.warning("Cannot start WS - websocket-client not installed")
            self._fallback_active = True
            if self.on_status:
                self.on_status("fallback_rest_polling")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-client")
        self._thread.start()
        logger.info(f"WS client started for {self.url}")

    def stop(self):
        with self._lock:
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
                # SECTION 7 AUDIT: Connection timeout
                self._ws.run_forever(
                    ping_interval=self._ping_interval,
                    ping_timeout=10,
                    connect_timeout=self._connect_timeout
                )
            except Exception as e:
                logger.error(f"WS connection error: {e}")

            with self._lock:
                if not self._running:
                    break
                if self._connected_since:
                    self._connection_duration = time.time() - self._connected_since
                self._reconnect_count += 1
                self._total_reconnects += 1
                if self._reconnect_count > self._max_reconnects:
                    logger.error(f"WS max reconnections ({self._max_reconnects}) exceeded - giving up")
                    self._fallback_active = True
                    if self.on_status:
                        self.on_status(f"max_reconnects_exceeded ({self._max_reconnects}) - fallback to REST")
                    self._running = False
                    break

            # SECTION 7 AUDIT: Exponential backoff with jitter
            jitter = random.uniform(0, self._backoff * 0.1)
            sleep_time = self._backoff + jitter
            logger.info(f"WS reconnecting in {sleep_time:.1f}s (attempt {self._reconnect_count}/{self._max_reconnects})...")
            if self.on_status:
                self.on_status(f"reconnecting in {sleep_time:.1f}s")
            time.sleep(sleep_time)
            with self._lock:
                if self._connection_duration < self._sustained_threshold:
                    self._backoff = min(self._backoff * 2, self._max_backoff)

    def _on_open(self, ws):
        logger.info(f"WS connected to {self.url}")
        with self._lock:
            # SECTION 7 AUDIT: Only reset backoff if previous connection was sustained
            if self._connection_duration >= self._sustained_threshold or self._reconnect_count == 0:
                self._backoff = 1.0
            self._reconnect_count = 0
            self._is_connected = True
            self._connected_since = time.time()
            self._last_message_time = time.time()
            self._fallback_active = False
        if self.on_status:
            self.on_status("connected")
        if self.on_connect:
            self.on_connect(ws)
        # SECTION 7 AUDIT: Re-subscribe after reconnection
        self._recover_subscriptions()

    def _on_msg(self, ws, message):
        try:
            data = json.loads(message)
            # SECTION 7 AUDIT: Message type validation
            if not isinstance(data, (dict, list)):
                logger.debug(f"WS unexpected message type: {type(data).__name__}")
                return
            with self._lock:
                self._last_message_time = time.time()
            if self.on_message:
                self.on_message(data)
        except json.JSONDecodeError:
            logger.debug(f"WS non-JSON message: {message[:100]}")
        except Exception as e:
            logger.error(f"WS message handler error: {e}")

    def _on_err(self, ws, error):
        logger.error(f"WS error: {error}")
        with self._lock:
            self._is_connected = False
        if self.on_status:
            self.on_status(f"error: {error}")

    def _on_close(self, ws, close_status, close_msg):
        logger.warning(f"WS closed: {close_status} {close_msg}")
        with self._lock:
            self._is_connected = False
            self._last_disconnect_time = time.time()
            if self._connected_since:
                self._connection_duration = time.time() - self._connected_since
            self._connected_since = None
        if self.on_status:
            self.on_status("disconnected")

    def send(self, data: dict):
        if self._ws:
            try:
                self._ws.send(json.dumps(data))
            except Exception as e:
                logger.error(f"WS send failed: {e}")

    @property
    def is_connected(self):
        with self._lock:
            return self._is_connected

    @property
    def is_fallback_active(self):
        with self._lock:
            return self._fallback_active

    def _recover_subscriptions(self):
        """SECTION 7 AUDIT: Re-subscribe to channels after reconnection."""
        if self._subscriptions:
            logger.info(f"WS recovering {len(self._subscriptions)} subscriptions")
            self._do_subscribe(self._subscriptions)

    def _do_subscribe(self, asset_ids: list):
        """Override in subclasses to implement subscription."""
        pass

    def unsubscribe(self, asset_ids: list):
        """SECTION 7 AUDIT: Unsubscribe from given asset IDs."""
        with self._lock:
            self._subscriptions = [a for a in self._subscriptions if a not in asset_ids]
        msg = {"type": "Unsubscribe", "assets_ids": asset_ids}
        self.send(msg)
        logger.info(f"WS unsubscribed from {len(asset_ids)} assets")

    def health_check(self) -> dict:
        """SECTION 7 AUDIT: Check connection health."""
        with self._lock:
            time_since_msg = time.time() - self._last_message_time if self._last_message_time else None
            is_stale = (
                self._is_connected and
                time_since_msg is not None and
                time_since_msg > self._stale_threshold
            )
            return {
                'is_connected': self._is_connected,
                'is_stale': is_stale,
                'time_since_last_message': round(time_since_msg, 1) if time_since_msg else None,
                'stale_threshold': self._stale_threshold,
                'fallback_active': self._fallback_active,
                'reconnect_count': self._reconnect_count,
            }

    def get_status(self):
        with self._lock:
            return {
                'url': self.url,
                'is_connected': self._is_connected,
                'is_running': self._running,
                'reconnect_count': self._reconnect_count,
                'total_reconnects': self._total_reconnects,
                'max_reconnects': self._max_reconnects,
                'backoff': round(self._backoff, 2),
                'connected_since': self._connected_since,
                'last_disconnect': self._last_disconnect_time,
                'ping_interval': self._ping_interval,
                'fallback_active': self._fallback_active,
                'connection_duration': round(self._connection_duration, 1),
                'sustained_threshold': self._sustained_threshold,
                'subscriptions_count': len(self._subscriptions),
                'last_message_time': self._last_message_time,
            }


class MarketWSClient(BaseWSClient):
    """Market channel WebSocket - order book updates."""

    def __init__(self, on_message=None, on_status=None, on_connect=None, url=None):
        from config.settings import WS_MARKET
        super().__init__(url or WS_MARKET, on_message, on_status, on_connect)

    def subscribe(self, asset_ids: list):
        with self._lock:
            self._subscriptions = list(set(self._subscriptions + asset_ids))
        msg = {"type": "Market", "assets_ids": asset_ids}
        self.send(msg)
        logger.info(f"WS subscribed to {len(asset_ids)} assets")

    def _do_subscribe(self, asset_ids: list):
        msg = {"type": "Market", "assets_ids": asset_ids}
        self.send(msg)
        logger.info(f"WS re-subscribed to {len(asset_ids)} assets after reconnection")


class UserWSClient(BaseWSClient):
    """User channel WebSocket - order fills, trades, balance updates."""

    def __init__(self, on_message=None, on_status=None, on_connect=None, url=None, auth_params=None):
        from config.settings import WS_USER
        super().__init__(url or WS_USER, on_message, on_status, on_connect)
        self.auth_params = auth_params or {}

    def _on_open(self, ws):
        super()._on_open(ws)
        if self.auth_params:
            self.send({"type": "User", **self.auth_params})
            logger.info("WS user channel authenticated")
