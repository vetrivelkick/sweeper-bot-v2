"""SECTION 19 AUDIT: Unit tests for modules/safety_rails.py."""
import pytest
import time
from modules.safety_rails import SafetyRails


class TestPreflight:
    def test_preflight_passes_paper(self, safety):
        ok, checks = safety.preflight_check()
        assert ok is True
        assert isinstance(checks, list)


class TestKillSwitch:
    def test_manual_kill(self, safety):
        safety.manual_kill("Test")
        assert safety.state.is_killed

    def test_check_kill_not_triggered(self, safety):
        assert not safety.check_kill_switch()[0]

    def test_check_kill_triggered(self, safety):
        safety.state.daily_pnl = -100.0
        assert safety.check_kill_switch()[0]


class TestExposure:
    def test_zero_exposure(self, safety):
        assert safety.get_exposure()['total_exposure'] == 0

    def test_blocks_over_limit(self, safety):
        safety.state.open_positions['c1'] = {'cost': 2000}
        assert not safety.check_exposure_before_order(100)[0]


class TestRiskScore:
    def test_zero_initially(self, safety):
        assert safety.get_risk_score() == 0.0

    def test_increases_with_losses(self, safety):
        safety._consecutive_losses = 3
        assert safety.get_risk_score() > 0

    def test_max_100(self, safety):
        safety._consecutive_losses = 100
        safety.state.rate_limit_429_count = 100
        safety.state.open_positions['c1'] = {'cost': 5000}
        assert safety.get_risk_score() <= 100


class TestSection18RiskControls:
    def test_check_risk_normal(self, safety):
        assert safety.check_risk_before_trade(50, 100)[0]

    def test_check_risk_kill_switch(self, safety):
        safety.state.is_killed = True
        safety.state.kill_reason = "Test"
        assert not safety.check_risk_before_trade(50, 100)[0]

    def test_trigger_circuit_breaker(self, safety):
        safety.trigger_circuit_breaker("Test")
        assert safety._circuit_breaker_active
        assert safety._circuit_breaker_until > time.time()

    def test_circuit_breaker_active(self, safety):
        safety.trigger_circuit_breaker("Test")
        active, remaining = safety.check_circuit_breaker()
        assert active and remaining > 0

    def test_circuit_breaker_expired(self, safety):
        safety.trigger_circuit_breaker("Test")
        safety._circuit_breaker_until = time.time() - 1
        active, _ = safety.check_circuit_breaker()
        assert not active
        assert not safety._circuit_breaker_active

    def test_circuit_breaker_blocks_trade(self, safety):
        safety.trigger_circuit_breaker("Test")
        assert not safety.check_risk_before_trade(50, 100)[0]

    def test_risk_adjusted_size_low(self, safety):
        s, c = safety.risk_adjusted_size(100, 99)
        assert s == 100 and c == 99

    def test_risk_adjusted_size_high(self, safety):
        safety._consecutive_losses = 5
        safety.state.rate_limit_429_count = 3
        s, c = safety.risk_adjusted_size(100, 99)
        assert s < 100 and c < 99

    def test_liquidity_sufficient(self, safety):
        assert safety.check_liquidity(500)[0]

    def test_liquidity_insufficient(self, safety):
        assert not safety.check_liquidity(50)[0]

    def test_auto_degrade_paper(self, safety):
        assert not safety.auto_degrade()[0]

    def test_stress_test_no_positions(self, safety):
        r = safety.stress_test()
        assert r['would_survive']

    def test_stress_test_with_positions(self, safety):
        safety.state.open_positions['c1'] = {'cost': 500, 'shares': 500}
        r = safety.stress_test()
        assert r['position_loss'] > 0
        assert r['gas_loss'] > 0

    def test_get_risk_status(self, safety):
        s = safety.get_risk_status()
        assert 'risk_score' in s
        assert s['within_limits']


class TestHealthCheck:
    def test_healthy(self, safety):
        assert safety.health_check()['status'] == 'healthy'

    def test_killed(self, safety):
        safety.state.is_killed = True
        assert safety.health_check()['status'] == 'killed'
