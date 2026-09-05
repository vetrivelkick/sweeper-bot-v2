"""SECTION 19 AUDIT: Unit tests for economics gate."""
import pytest
from config import BUY_PRICE
from modules.safety_rails import SafetyRails


class TestEconomicsGate:
    def test_gate_passes(self, safety):
        assert safety.check_economics_gate(0.99, 100, category="crypto", is_maker=True)[0]

    def test_gate_bad_price(self, safety):
        assert not safety.check_economics_gate(0.50, 100, is_maker=True)[0]

    def test_gate_too_small(self, safety):
        assert not safety.check_economics_gate(0.99, 1, is_maker=True)[0]

    def test_gate_killed(self, safety):
        safety.state.is_killed = True
        safety.state.kill_reason = "Test"
        assert not safety.check_economics_gate(0.99, 100, is_maker=True)[0]

    def test_validate_config(self, safety):
        assert safety.validate_economics_config()[0]

    def test_get_metrics(self, safety):
        m = safety.get_economics_metrics(category="crypto", is_maker=True)
        assert m['buy_price'] == BUY_PRICE
        assert m['fee_rate'] == 0.07
        assert m['profitable']

    def test_break_even_maker(self, safety):
        be = safety.calculate_break_even(0.99, is_maker=True)
        assert 0.99 < be < 1.0

    def test_slippage(self, safety):
        r = safety.estimate_slippage(100, 1000)
        assert r['within_threshold']
