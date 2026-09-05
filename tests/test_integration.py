"""SECTION 19 AUDIT: Integration tests for paper trading flow."""
import sys, os, time
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import SweeperConfig, fee_per_share, GAS_PER_SHARE, BUY_PRICE, LOSER_MAX_PRICE
from modules.safety_rails import SafetyRails
from modules.ledger import DoubleEntryLedger


class TestPaperTradingIntegration:
    def test_full_trade_cycle(self):
        config = SweeperConfig(paper_mode=True)
        safety = SafetyRails(config)
        ledger = DoubleEntryLedger()
        ok, _ = safety.preflight_check()
        assert ok
        shares = 100
        ok, _ = safety.check_economics_gate(BUY_PRICE, shares, category="crypto", is_maker=True)
        assert ok
        ok, _ = safety.check_risk_before_trade(BUY_PRICE * shares, shares)
        assert ok
        gross = (1.0 - BUY_PRICE) * shares
        fee = fee_per_share(BUY_PRICE, is_maker=True) * shares
        loser_cost = LOSER_MAX_PRICE * shares
        gas = GAS_PER_SHARE * shares
        net_pnl = gross - fee - loser_cost - gas
        ledger.record_buy_winning(1, BUY_PRICE, shares, is_maker=True)
        ledger.record_buy_loser(1, LOSER_MAX_PRICE, shares)
        ledger.record_fee(1, fee, is_maker=True)
        ledger.record_gas(1, gas)
        ledger.record_merge(1, shares, BUY_PRICE * shares, LOSER_MAX_PRICE * shares)
        assert ledger.verify_balanced()[0]
        assert abs(ledger.get_pnl() - net_pnl) < 0.01
        safety.update_scoreboard(buys=[{}], redeems=[{}], merges=[{"amount": shares}], net_pnl=net_pnl)
        tp = safety.get_true_pnl()
        assert tp['total_buys'] == 1
        assert abs(tp['true_pnl'] - net_pnl) < 0.01

    def test_risk_gate_blocks(self):
        safety = SafetyRails(SweeperConfig(paper_mode=True))
        safety.state.is_killed = True
        safety.state.kill_reason = "Test"
        assert not safety.check_risk_before_trade(99, 100)[0]

    def test_circuit_breaker_blocks_then_expires(self):
        safety = SafetyRails(SweeperConfig(paper_mode=True))
        safety.trigger_circuit_breaker("Test")
        assert not safety.check_risk_before_trade(50, 100)[0]
        safety._circuit_breaker_until = time.time() - 1
        assert safety.check_risk_before_trade(50, 100)[0]

    def test_stress_test_survives(self):
        safety = SafetyRails(SweeperConfig(paper_mode=True))
        assert safety.stress_test()['would_survive']

    def test_multiple_trades_accumulate(self):
        safety = SafetyRails(SweeperConfig(paper_mode=True))
        total = 0.0
        for _ in range(5):
            shares = 100
            net = (1.0 - BUY_PRICE) * shares - LOSER_MAX_PRICE * shares - GAS_PER_SHARE * shares
            total += net
            safety.update_scoreboard(buys=[{}], redeems=[{}], merges=[{"amount": shares}], net_pnl=net)
        tp = safety.get_true_pnl()
        assert tp['total_buys'] == 5
        assert abs(tp['true_pnl'] - total) < 0.01
