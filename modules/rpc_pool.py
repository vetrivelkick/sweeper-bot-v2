"""
Sweeper Bot V2 - RPC Connection Pool (Phase 8)

Production hardening: Multi-endpoint RPC pool with automatic failover.
Avoids single-endpoint rate limits and improves reliability.

Features:
- Round-robin endpoint selection with health tracking
- Automatic failover on connection errors
- Request retry with exponential backoff
- Latency tracking per endpoint
- Circuit breaker per endpoint (trips after N consecutive failures)

Usage:
    pool = RpcPool(["https://polygon-rpc.com", "https://rpc-mainnet.matic.network"])
    result = pool.call("eth_blockNumber", [])
    pool.get_health()
"""
import time, logging, random
from typing import Optional, List, Dict
from dataclasses import dataclass, field

logger = logging.getLogger("sweeper.rpc")


@dataclass
class EndpointHealth:
    url: str
    healthy: bool = True
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    avg_latency_ms: float = 0.0
    last_failure_time: float = 0.0
    circuit_open_until: float = 0.0  # Timestamp until circuit breaker resets


class RpcPool:
    """Multi-endpoint RPC pool with failover and circuit breaker."""

    def __init__(self, endpoints: List[str], max_retries: int = 3,
                 circuit_threshold: int = 5, circuit_reset_seconds: float = 60.0):
        if not endpoints:
            raise ValueError("At least one RPC endpoint required")
        self._endpoints = {url: EndpointHealth(url=url) for url in endpoints}
        self._endpoint_list = list(endpoints)
        self._rr_index = 0
        self._max_retries = max_retries
        self._circuit_threshold = circuit_threshold
        self._circuit_reset_seconds = circuit_reset_seconds
        self._web3 = None
        self._init_web3()

    def _init_web3(self):
        """Initialize Web3 with first healthy endpoint."""
        try:
            from web3 import Web3
            for url in self._endpoint_list:
                try:
                    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
                    if w3.is_connected():
                        self._web3 = w3
                        logger.info(f"[RPC] Connected to {url}")
                        return
                except Exception as e:
                    logger.warning(f"[RPC] Failed to connect {url}: {e}")
                    self._endpoints[url].healthy = False
            logger.error("[RPC] All endpoints failed initial connection")
        except ImportError:
            logger.warning("[RPC] web3 not installed, RPC pool disabled")

    def _get_healthy_endpoint(self) -> Optional[str]:
        """Get next healthy endpoint via round-robin."""
        now = time.time()
        for _ in range(len(self._endpoint_list)):
            url = self._endpoint_list[self._rr_index % len(self._endpoint_list)]
            self._rr_index += 1
            health = self._endpoints[url]
            # Check circuit breaker
            if health.circuit_open_until > now:
                continue
            if not health.healthy:
                # Check if it's time to retry
                if now - health.last_failure_time > self._circuit_reset_seconds:
                    health.healthy = True
                    health.consecutive_failures = 0
                    logger.info(f"[RPC] Retrying endpoint {url}")
                else:
                    continue
            return url
        return None

    def _mark_failure(self, url: str, error: str):
        """Mark an endpoint as failed and potentially open circuit breaker."""
        health = self._endpoints[url]
        health.consecutive_failures += 1
        health.total_failures += 1
        health.last_failure_time = time.time()
        if health.consecutive_failures >= self._circuit_threshold:
            health.healthy = False
            health.circuit_open_until = time.time() + self._circuit_reset_seconds
            logger.warning(f"[RPC] Circuit breaker opened for {url} ({health.consecutive_failures} failures)")
        else:
            logger.warning(f"[RPC] Endpoint {url} failed ({health.consecutive_failures}/{self._circuit_threshold}): {error}")

    def _mark_success(self, url: str, latency_ms: float):
        """Mark an endpoint as successful."""
        health = self._endpoints[url]
        health.consecutive_failures = 0
        health.total_requests += 1
        health.healthy = True
        # Exponential moving average for latency
        alpha = 0.3
        health.avg_latency_ms = (1 - alpha) * health.avg_latency_ms + alpha * latency_ms

    def call(self, method: str, params: list = None, retries: int = None) -> Optional[dict]:
        """Make an RPC call with automatic failover."""
        if params is None:
            params = []
        max_retries = retries or self._max_retries
        backoff = 1.0

        for attempt in range(max_retries):
            url = self._get_healthy_endpoint()
            if url is None:
                logger.error("[RPC] No healthy endpoints available")
                return None

            try:
                from web3 import Web3
                start = time.time()
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
                result = w3.provider.make_request(method, params)
                latency = (time.time() - start) * 1000
                self._mark_success(url, latency)
                return result
            except Exception as e:
                self._mark_failure(url, str(e))
                if attempt < max_retries - 1:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)

        logger.error(f"[RPC] All retries exhausted for {method}")
        return None

    def get_block_number(self) -> Optional[int]:
        result = self.call("eth_blockNumber")
        if result and "result" in result:
            return int(result["result"], 16)
        return None

    def get_health(self) -> List[Dict]:
        """Return health status of all endpoints."""
        return [
            {
                "url": h.url,
                "healthy": h.healthy,
                "consecutive_failures": h.consecutive_failures,
                "total_requests": h.total_requests,
                "total_failures": h.total_failures,
                "avg_latency_ms": round(h.avg_latency_ms, 2),
                "circuit_open": h.circuit_open_until > time.time(),
            }
            for h in self._endpoints.values()
        ]

    def get_best_endpoint(self) -> Optional[str]:
        """Return the endpoint with lowest average latency."""
        healthy = [h for h in self._endpoints.values() if h.healthy and h.circuit_open_until <= time.time()]
        if not healthy:
            return None
        return min(healthy, key=lambda h: h.avg_latency_ms if h.avg_latency_ms > 0 else float("inf")).url
