"""
Sweeper Bot V2 - Rate Limit Manager

AUDIT FIX #19: Adaptive rate limiting, 429 counters, circuit breaker per bucket.
- Adaptive: auto-reduce bucket limits when 429s are frequent
- 429 counters per bucket for metrics integration
- Circuit breaker: pause bucket for longer after N consecutive 429s
- Rate limit alerting via callback
"""
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable

logger = logging.getLogger("sweeper.ratelimit")


@dataclass
class RateBucket:
    name: str
    max_per_min: int
    original_max: int = 0
    requests: deque = field(default_factory=deque)
    last_429: float = 0.0
    retry_after: float = 0.0
    last_425: float = 0.0
    # AUDIT FIX #19: New fields
    total_429s: int = 0
    total_425s: int = 0
    consecutive_429s: int = 0
    circuit_breaker_until: float = 0.0
    adaptive_reduction: float = 1.0  # 1.0 = full capacity, 0.5 = half


class RateLimitManager:
    def __init__(self, config):
        self.config = config
        self._seen_order_ids = set()
        headroom = config.rate_limit_headroom
        self.buckets = {
            'order': RateBucket('CLOB POST /order', int(config.rate_limit_order_per_min * headroom),
                                original_max=int(config.rate_limit_order_per_min * headroom)),
            'book': RateBucket('CLOB /book', int(config.rate_limit_book_per_min * headroom),
                               original_max=int(config.rate_limit_book_per_min * headroom)),
            'gamma': RateBucket('Gamma /markets', int(config.rate_limit_gamma_per_min * headroom),
                               original_max=int(config.rate_limit_gamma_per_min * headroom)),
            'api_key': RateBucket('API key endpoints', int(80 * headroom),
                                  original_max=int(80 * headroom)),
            'relayer': RateBucket('Relayer /submit', int(20 * headroom),
                                  original_max=int(20 * headroom)),
        }
        # AUDIT FIX #19: Circuit breaker settings
        self._circuit_threshold = 5  # consecutive 429s before circuit breaker
        self._circuit_pause_seconds = 30.0  # pause duration when circuit trips
        self._adaptive_window = 120.0  # window to check 429 frequency
        self._adaptive_threshold = 3  # 429s in window to trigger reduction
        self._adaptive_recovery_time = 300.0  # 5 min to recover full capacity
        self._alert_callback: Optional[Callable] = None

    def set_alert_callback(self, callback: Callable):
        """AUDIT FIX #19: Set callback for rate limit alerts."""
        self._alert_callback = callback

    def _prune(self, bucket: RateBucket):
        cutoff = time.time() - 60
        while bucket.requests and bucket.requests[0] < cutoff:
            bucket.requests.popleft()

    def _check_adaptive(self, bucket: RateBucket):
        """AUDIT FIX #19: Check if adaptive reduction should apply or recover."""
        now = time.time()
        # Check if we should reduce capacity (too many 429s recently)
        if bucket.consecutive_429s >= self._adaptive_threshold:
            new_reduction = max(0.25, bucket.adaptive_reduction * 0.5)
            if new_reduction != bucket.adaptive_reduction:
                bucket.adaptive_reduction = new_reduction
                bucket.max_per_min = max(1, int(bucket.original_max * new_reduction))
                logger.warning(f"Adaptive rate limit: {bucket.name} reduced to {bucket.max_per_min}/min "
                               f"({new_reduction*100:.0f}% capacity, {bucket.consecutive_429s} consecutive 429s)")
                if self._alert_callback:
                    self._alert_callback('RATE_LIMIT_REDUCED', bucket.name, bucket.max_per_min)
        # Check if we should recover capacity (no recent 429s)
        elif bucket.adaptive_reduction < 1.0 and (now - bucket.last_429) > self._adaptive_recovery_time:
            bucket.adaptive_reduction = min(1.0, bucket.adaptive_reduction * 2)
            bucket.max_per_min = max(1, int(bucket.original_max * bucket.adaptive_reduction))
            logger.info(f"Adaptive rate limit: {bucket.name} recovered to {bucket.max_per_min}/min "
                        f"({bucket.adaptive_reduction*100:.0f}% capacity)")
            if self._alert_callback:
                self._alert_callback('RATE_LIMIT_RECOVERED', bucket.name, bucket.max_per_min)

    def can_request(self, bucket_name: str) -> bool:
        bucket = self.buckets.get(bucket_name)
        if not bucket:
            return True
        now = time.time()
        # AUDIT FIX #19: Check circuit breaker
        if bucket.circuit_breaker_until > now:
            return False
        self._prune(bucket)
        if bucket.retry_after > now:
            return False
        self._check_adaptive(bucket)
        return len(bucket.requests) < bucket.max_per_min

    def record_request(self, bucket_name: str):
        bucket = self.buckets.get(bucket_name)
        if bucket:
            bucket.requests.append(time.time())

    def handle_429(self, bucket_name: str, retry_after_seconds: float = 1.0):
        bucket = self.buckets.get(bucket_name)
        if bucket:
            bucket.last_429 = time.time()
            bucket.retry_after = time.time() + retry_after_seconds
            bucket.total_429s += 1
            bucket.consecutive_429s += 1
            # AUDIT FIX #19: Circuit breaker on consecutive 429s
            if bucket.consecutive_429s >= self._circuit_threshold:
                bucket.circuit_breaker_until = time.time() + self._circuit_pause_seconds
                logger.error(f"Circuit breaker OPEN for {bucket.name} ({bucket.consecutive_429s} consecutive 429s, "
                             f"paused {self._circuit_pause_seconds}s)")
                if self._alert_callback:
                    self._alert_callback('CIRCUIT_BREAKER_OPEN', bucket.name, self._circuit_pause_seconds)
            else:
                logger.warning(f"429 on {bucket.name} ({bucket.consecutive_429s} consecutive), retry after {retry_after_seconds}s")

    def handle_425(self, bucket_name: str = 'order'):
        """Track 425 (engine restarting) events and set a cooldown."""
        bucket = self.buckets.get(bucket_name)
        if bucket:
            bucket.last_425 = time.time()
            bucket.total_425s += 1
            bucket.retry_after = time.time() + 5.0
            logger.warning(f"425 on {bucket.name}, engine restarting - 5s cooldown")

    def record_success(self, bucket_name: str):
        """AUDIT FIX #19: Reset consecutive 429 counter on successful request."""
        bucket = self.buckets.get(bucket_name)
        if bucket:
            bucket.consecutive_429s = 0

    def is_duplicate_order(self, order_id: str) -> bool:
        """Check if an order ID was already submitted (duplicate prevention)."""
        return order_id in self._seen_order_ids

    def record_order_id(self, order_id: str):
        """Track submitted order IDs to prevent duplicates."""
        self._seen_order_ids.add(order_id)
        if len(self._seen_order_ids) > 1000:
            self._seen_order_ids = set(list(self._seen_order_ids)[-500:])

    def remaining(self, bucket_name: str) -> int:
        bucket = self.buckets.get(bucket_name)
        if not bucket:
            return 999
        self._prune(bucket)
        return bucket.max_per_min - len(bucket.requests)

    def status(self) -> dict:
        """AUDIT FIX #19: Enhanced status with 429 counts, circuit breaker, adaptive."""
        now = time.time()
        return {
            name: {
                'remaining': self.remaining(name),
                'max': b.max_per_min,
                'original_max': b.original_max,
                'adaptive_capacity': round(b.adaptive_reduction * 100, 1),
                'total_429s': b.total_429s,
                'total_425s': b.total_425s,
                'consecutive_429s': b.consecutive_429s,
                'circuit_open': b.circuit_breaker_until > now,
                'retry_after': b.retry_after > now,
            }
            for name, b in self.buckets.items()
        }
