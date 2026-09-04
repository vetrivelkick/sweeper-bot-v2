"""
Sweeper Bot V2 - Rate Limit Manager

AUDIT FIX #19: Adaptive rate limiting, 429 counters, circuit breaker per bucket.
- Adaptive: auto-reduce bucket limits when 429s are frequent
- 429 counters per bucket for metrics integration
- Circuit breaker: pause bucket for longer after N consecutive 429s
- Rate limit alerting via callback
SECTION 10 AUDIT: Retry-After header parsing, backoff with jitter, state persistence,
                 metrics export, Data API bucket, WS reconnection bucket, bucket cleanup
"""
import time, random, logging, json, os
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, Any

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
            # SECTION 10 AUDIT: Data API bucket
            'data': RateBucket('Data API /markets', int(60 * headroom),
                              original_max=int(60 * headroom)),
            # SECTION 10 AUDIT: WebSocket reconnection bucket
            'ws_reconnect': RateBucket('WS reconnect', int(10 * headroom),
                                       original_max=int(10 * headroom)),
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
            # SECTION 10 AUDIT: Add jitter to retry_after to avoid thundering herd
            jitter = random.uniform(0, retry_after_seconds * 0.1)
            bucket.retry_after = time.time() + retry_after_seconds + jitter
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

    def parse_retry_after(self, headers: dict) -> float:
        """SECTION 10 AUDIT: Parse Retry-After header from HTTP response."""
        retry_after = headers.get('Retry-After') or headers.get('retry-after')
        if retry_after:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass
        return 1.0

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
            # SECTION 10 AUDIT: Record request timestamp on success
            self.record_request(bucket_name)

    def is_duplicate_order(self, order_id: str) -> bool:
        """Check if an order ID was already submitted (duplicate prevention)."""
        return order_id in self._seen_order_ids

    def record_order_id(self, order_id: str):
        """Track submitted order IDs to prevent duplicates."""
        self._seen_order_ids.add(order_id)
        if len(self._seen_order_ids) > 1000:
            self._seen_order_ids = set(list(self._seen_order_ids)[-500:])

    def save_state(self, filepath: str = 'data/rate_limit_state.json'):
        """SECTION 10 AUDIT: Persist rate limit state for crash recovery."""
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            state = {}
            for name, b in self.buckets.items():
                state[name] = {
                    'total_429s': b.total_429s,
                    'total_425s': b.total_425s,
                    'adaptive_reduction': b.adaptive_reduction,
                    'max_per_min': b.max_per_min,
                }
            with open(filepath, 'w') as f:
                json.dump(state, f, indent=2)
            logger.info(f"Rate limit state saved to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save rate limit state: {e}")

    def load_state(self, filepath: str = 'data/rate_limit_state.json'):
        """SECTION 10 AUDIT: Load rate limit state for crash recovery."""
        try:
            if not os.path.exists(filepath):
                return
            with open(filepath, 'r') as f:
                state = json.load(f)
            for name, s in state.items():
                b = self.buckets.get(name)
                if b:
                    b.total_429s = s.get('total_429s', 0)
                    b.total_425s = s.get('total_425s', 0)
                    b.adaptive_reduction = s.get('adaptive_reduction', 1.0)
                    b.max_per_min = s.get('max_per_min', b.original_max)
            logger.info(f"Rate limit state loaded from {filepath}")
        except Exception as e:
            logger.error(f"Failed to load rate limit state: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """SECTION 10 AUDIT: Export rate limit metrics for monitoring."""
        now = time.time()
        metrics = {}
        for name, b in self.buckets.items():
            metrics[name] = {
                'remaining': self.remaining(name),
                'max_per_min': b.max_per_min,
                'original_max': b.original_max,
                'adaptive_capacity_pct': round(b.adaptive_reduction * 100, 1),
                'total_429s': b.total_429s,
                'total_425s': b.total_425s,
                'consecutive_429s': b.consecutive_429s,
                'circuit_breaker_open': b.circuit_breaker_until > now,
                'retry_after_active': b.retry_after > now,
                'current_usage': len(b.requests),
            }
        return metrics

    def cleanup_buckets(self):
        """SECTION 10 AUDIT: Clean up expired request timestamps from all buckets."""
        for bucket in self.buckets.values():
            self._prune(bucket)

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
