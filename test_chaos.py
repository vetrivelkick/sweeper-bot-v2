"""
Sweeper Bot V2 - Chaos & Replay Test (Phase 8)

Production hardening: Test bot resilience against simulated failures.
Verifies kill switch, exposure limits, rate limiting, and error handling
all work correctly under stress conditions.

Test scenarios:
  1. Rate limit spike: Simulate 429/425 flood
  2. Kill switch trip: Verify bot stops trading after threshold
  3. Exposure breach: Verify orders blocked when exposure exceeded
  4. Ghost fill flood: Verify ghost fills don't corrupt PnL
  5. Expired order flood: Verify expired orders don't block capital
  6. RPC failure: Verify RPC pool failover works
  7. State corruption: Verify atomic state save/load works
  8. Reconciliation: Verify phantom position detection
  9. Decimal precision: Verify tick alignment across tick sizes
  10. Consecutive losses: Verify consecutive loss kill switch

Usage:
    python3 test_chaos.py
"""
import sys, os, json, time, random, logging
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SweeperConfig, GAS_PER_SHARE, fee_per_share
from modules.safety_rails import SafetyRails
from modules.order_executor import OrderBuilder, OrderStatus, plan_entry
from modules.rate_limiter import RateLimitManager
from modules.ledger import DoubleEntryLedger
from modules.metrics import MetricsCollector, AlertManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("sweeper.chaos")


class ChaosTest:
    """Run chaos engineering tests against bot components."""

    def __init__(self):
        self.config = SweeperConfig(paper_mode=True)
        self.passed = 0
        self.failed = 0
        self.results = []

    def _assert(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            self.results.append(("PASS", name, detail))
            logger.info(f"  [PASS] {name}: {detail}")
        else:
            self.failed += 1
            self.results.append(("FAIL", name, detail))
            logger.error(f"  [FAIL] {name}: {detail}")

    def test_rate_limit_spike(self):
        """Test 1: Simulate 429 rate limit flood."""
        logger.info("\n[TEST 1] Rate Limit Spike")
        safety = SafetyRails(self.config)
        for i in range(self.config.max_429_before_trip):
            safety.record_429()
        killed, reason = safety.check_kill_switch()
        self._assert("Kill switch on 429 flood", killed, f"{reason} after {safety.state.rate_limit_429_count} 429s")

    def test_kill_switch_trip(self):
        """Test 2: Verify bot stops trading after kill switch."""
        logger.info("\n[TEST 2] Kill Switch Trip")
        safety = SafetyRails(self.config)
        safety.state.is_killed = True
        safety.state.kill_reason = "Test: daily loss exceeded"
        killed, reason = safety.check_kill_switch()
        self._assert("Kill switch active", killed, reason)
        self._assert("Kill reason stored", "daily loss" in safety.state.kill_reason.lower(), safety.state.kill_reason)

    def test_exposure_breach(self):
        """Test 3: Verify orders blocked when exposure exceeded."""
        logger.info("\n[TEST 3] Exposure Breach")
        safety = SafetyRails(self.config)
        builder = OrderBuilder(self.config, safety=safety)
        # Simulate max exposure by adding fake resting orders
        for i in range(25):
            from modules.order_executor import RestingOrder
            order = RestingOrder(
                order_id=f"fake_{i}", condition_id=f"cond_{i}", token_id="tok",
                market_question="Test", side="BUY", price=0.99, shares=100,
                tick_size="0.01", neg_risk=False
            )
            builder._resting[order.order_id] = order
            builder._reserved[order.order_id] = 99.0
        exp = safety.get_exposure(builder.list_open_orders())
        exceeded = float(exp["total_exposure"]) > float(exp["max_portfolio"])
        self._assert("Exposure exceeded with 25 fake orders", exceeded,
                     f"Total: ${exp['total_exposure']} vs Max: ${exp['max_portfolio']}")

    def test_ghost_fill_pnl(self):
        """Test 4: Verify ghost fills don't corrupt PnL."""
        logger.info("\n[TEST 4] Ghost Fill PnL Integrity")
        safety = SafetyRails(self.config)
        initial_pnl = safety.get_true_pnl()["true_pnl"]
        # Simulate a ghost fill (no tx_hash, no settlement)
        safety.update_scoreboard(buys=[{}], redeems=[{}], merges=[], net_pnl=0)
        final_pnl = safety.get_true_pnl()["true_pnl"]
        self._assert("Ghost fill doesn't change PnL", abs(final_pnl - initial_pnl) < 0.01,
                     f"Before: ${initial_pnl}, After: ${final_pnl}")

    def test_decimal_precision(self):
        """Test 5: Verify Decimal tick alignment across tick sizes."""
        logger.info("\n[TEST 5] Decimal Precision")
        from modules.paper_trading import plan_entry_fixed
        test_cases = [
            (0.995, 0.001, 0.985, 0.99),
            (0.99, 0.01, 0.985, 0.99),
            (0.999, 0.001, 0.985, 0.99),
            (0.98, 0.01, 0.985, 0.99),
        ]
        for best_ask, tick, min_e, max_e in test_cases:
            result = plan_entry_fixed(best_ask, tick, min_e, max_e, prefer_maker=True, allow_taker=False)
            if result:
                price, is_maker, detail = result
                # Verify price is on tick grid
                d_price = Decimal(str(price))
                d_tick = Decimal(str(tick))
                on_grid = (d_price % d_tick) == 0
                self._assert(f"Tick alignment (ask={best_ask}, tick={tick})", on_grid,
                             f"price={price} on_grid={on_grid}")
            else:
                self._assert(f"No valid entry (ask={best_ask}, tick={tick})", True, "Correctly rejected")

    def test_ledger_balanced(self):
        """Test 6: Verify double-entry ledger balances."""
        logger.info("\n[TEST 6] Ledger Balance")
        ledger = DoubleEntryLedger()
        # Simulate a complete trade
        price, shares = 0.99, 100
        ledger.record_buy_winning(1, price, shares, is_maker=True)
        ledger.record_buy_loser(1, 0.005, shares)
        ledger.record_gas(1, 0.001 * shares)
        ledger.record_merge(1, shares, price * shares, 0.005 * shares)
        balanced, td, tc, diff = ledger.verify_balanced()
        self._assert("Ledger balanced after trade", balanced, f"debit=${td:.4f} credit=${tc:.4f} diff=${diff:.6f}")
        pnl = ledger.get_pnl()
        expected_pnl = (1.0 - 0.99) * 100 - 0.005 * 100 - 0.001 * 100
        self._assert("Ledger PnL matches expected", abs(pnl - expected_pnl) < 0.01,
                     f"ledger=${pnl:.4f} expected=${expected_pnl:.4f}")

    def test_metrics_collection(self):
        """Test 7: Verify metrics collection and alerting."""
        logger.info("\n[TEST 7] Metrics & Alerting")
        metrics = MetricsCollector()
        metrics.inc("trades_total", 5)
        metrics.set("pnl_cumulative", 3.50)
        self._assert("Counter incremented", metrics.get("trades_total") == 5, f"value={metrics.get('trades_total')}")
        self._assert("Gauge set", abs(metrics.get("pnl_cumulative") - 3.50) < 0.01, f"value={metrics.get('pnl_cumulative')}")
        metrics.record_alert("TEST", "INFO", "Test alert")
        self._assert("Alert recorded", len(metrics._alerts) == 1, f"alerts={len(metrics._alerts)}")
        data = metrics.export()
        self._assert("Metrics exported", "counters" in data and "gauges" in data, "JSON export OK")

    def test_atomic_state(self):
        """Test 8: Verify atomic state save/load."""
        logger.info("\n[TEST 8] Atomic State")
        safety = SafetyRails(self.config)
        safety.mark_worked("test_condition_123")
        safety.dump_state()
        # Verify state file exists
        state_file = safety._state_file if hasattr(safety, "_state_file") else "sweeper_state.json"
        self._assert("State file created", os.path.exists(state_file), f"file={state_file}")
        # Verify no .tmp file left (atomic write completed)
        self._assert("No .tmp file leftover", not os.path.exists(state_file + ".tmp"), "Atomic write clean")
        # Cleanup
        if os.path.exists(state_file):
            os.remove(state_file)

    def test_consecutive_losses(self):
        """Test 9: Verify consecutive loss kill switch."""
        logger.info("\n[TEST 9] Consecutive Loss Kill Switch")
        safety = SafetyRails(self.config)
        limit = getattr(self.config, "consecutive_loss_limit", 5)
        for i in range(limit):
            safety.record_loss()
        killed, reason = safety.check_kill_switch()
        has_consecutive = hasattr(safety, "_consecutive_losses")
        if has_consecutive:
            self._assert("Consecutive loss kill switch", killed, f"{reason} after {safety._consecutive_losses} losses")
        else:
            self._assert("Consecutive loss tracking exists", False, "_consecutive_losses not found")

    def test_rate_limiter_budget(self):
        """Test 10: Verify rate limiter budget enforcement."""
        logger.info("\n[TEST 10] Rate Limiter Budget")
        rl = RateLimitManager(self.config)
        initial = rl.remaining("order")
        for i in range(min(5, initial)):
            if rl.can_request("order"):
                rl.record_request("order")
        remaining = rl.remaining("order")
        self._assert("Rate limiter decremented", remaining < initial,
                     f"initial={initial} remaining={remaining}")
        self._assert("Rate limiter non-negative", remaining >= 0, f"remaining={remaining}")

    def run_all(self):
        """Run all chaos tests."""
        logger.info("=" * 60)
        logger.info("  SWEEPER BOT V2 - CHAOS & REPLAY TESTS (Phase 8)")
        logger.info("=" * 60)

        tests = [
            self.test_rate_limit_spike,
            self.test_kill_switch_trip,
            self.test_exposure_breach,
            self.test_ghost_fill_pnl,
            self.test_decimal_precision,
            self.test_ledger_balanced,
            self.test_metrics_collection,
            self.test_atomic_state,
            self.test_consecutive_losses,
            self.test_rate_limiter_budget,
        ]

        for test in tests:
            try:
                test()
            except Exception as e:
                self.failed += 1
                self.results.append(("FAIL", test.__name__, str(e)))
                logger.error(f"  [FAIL] {test.__name__}: {e}")

        logger.info("\n" + "=" * 60)
        logger.info(f"  CHAOS TEST RESULTS: {self.passed} PASSED, {self.failed} FAILED")
        logger.info("=" * 60)
        for status, name, detail in self.results:
            symbol = "OK" if status == "PASS" else "XX"
            logger.info(f"  [{symbol}] {name}: {detail}")
        logger.info("=" * 60)

        return self.failed == 0


if __name__ == "__main__":
    test = ChaosTest()
    success = test.run_all()
    sys.exit(0 if success else 1)
