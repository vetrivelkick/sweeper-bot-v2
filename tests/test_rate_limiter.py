"""SECTION 19 AUDIT: Unit tests for rate limiter."""
import pytest
from config import SweeperConfig
from modules.rate_limiter import RateLimitManager


class TestRateLimiter:
    def test_initial_budget(self, config):
        assert RateLimitManager(config).remaining("order") > 0

    def test_can_request(self, config):
        assert RateLimitManager(config).can_request("order")

    def test_decrements(self, config):
        rl = RateLimitManager(config)
        initial = rl.remaining("order")
        rl.record_request("order")
        assert rl.remaining("order") < initial

    def test_exhausted(self, config):
        rl = RateLimitManager(config)
        for _ in range(rl.remaining("order")):
            if rl.can_request("order"):
                rl.record_request("order")
        assert not rl.can_request("order")

    def test_multiple_buckets(self, config):
        rl = RateLimitManager(config)
        assert rl.remaining("order") > 0
        assert rl.remaining("book") > 0
        assert rl.remaining("gamma") > 0
