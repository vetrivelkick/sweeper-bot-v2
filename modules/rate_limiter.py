"""Sweeper Bot V2 - Rate Limit Manager"""
import time
import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger("sweeper.ratelimit")

@dataclass
class RateBucket:
    name: str
    max_per_min: int
    requests: deque
    last_429: float = 0.0
    retry_after: float = 0.0
    last_425: float = 0.0  # P1: Track 425 (engine restarting) events

class RateLimitManager:
    def __init__(self, config):
        self.config = config
        self._seen_order_ids = set()  # P1: Duplicate order ID prevention
        headroom = config.rate_limit_headroom
        self.buckets = {
            'order': RateBucket('CLOB POST /order', int(config.rate_limit_order_per_min * headroom), deque()),
            'book': RateBucket('CLOB /book', int(config.rate_limit_book_per_min * headroom), deque()),
            'gamma': RateBucket('Gamma /markets', int(config.rate_limit_gamma_per_min * headroom), deque()),
            'api_key': RateBucket('API key endpoints', int(80 * headroom), deque()),
            'relayer': RateBucket('Relayer /submit', int(20 * headroom), deque()),
        }

    def _prune(self, bucket: RateBucket):
        cutoff = time.time() - 60
        while bucket.requests and bucket.requests[0] < cutoff:
            bucket.requests.popleft()

    def can_request(self, bucket_name: str) -> bool:
        bucket = self.buckets.get(bucket_name)
        if not bucket:
            return True
        self._prune(bucket)
        if bucket.retry_after > time.time():
            return False
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
            logger.warning(f"429 on {bucket.name}, retry after {retry_after_seconds}s")

    def handle_425(self, bucket_name: str = 'order'):
        """P1: Track 425 (engine restarting) events and set a cooldown."""
        bucket = self.buckets.get(bucket_name)
        if bucket:
            bucket.last_425 = time.time()
            bucket.retry_after = time.time() + 5.0  # 5s cooldown for engine restart
            logger.warning(f"425 on {bucket.name}, engine restarting - 5s cooldown")

    def is_duplicate_order(self, order_id: str) -> bool:
        """P1: Check if an order ID was already submitted (duplicate prevention)."""
        return order_id in self._seen_order_ids

    def record_order_id(self, order_id: str):
        """P1: Track submitted order IDs to prevent duplicates."""
        self._seen_order_ids.add(order_id)
        # Keep only last 1000 order IDs to avoid unbounded growth
        if len(self._seen_order_ids) > 1000:
            self._seen_order_ids = set(list(self._seen_order_ids)[-500:])

    def remaining(self, bucket_name: str) -> int:
        bucket = self.buckets.get(bucket_name)
        if not bucket:
            return 999
        self._prune(bucket)
        return bucket.max_per_min - len(bucket.requests)

    def status(self) -> dict:
        return {name: {'remaining': self.remaining(name), 'max': b.max_per_min} for name, b in self.buckets.items()}