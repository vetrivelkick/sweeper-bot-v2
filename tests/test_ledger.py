"""SECTION 19 AUDIT: Unit tests for double-entry ledger."""
import pytest
from modules.ledger import DoubleEntryLedger


class TestLedger:
    def test_empty_balanced(self):
        assert DoubleEntryLedger().verify_balanced()[0]

    def test_buy_winning(self):
        l = DoubleEntryLedger()
        l.record_buy_winning(1, 0.99, 100, is_maker=True)
        assert l.verify_balanced()[0]

    def test_full_trade(self):
        l = DoubleEntryLedger()
        l.record_buy_winning(1, 0.99, 100, is_maker=True)
        l.record_buy_loser(1, 0.005, 100)
        l.record_gas(1, 0.1)
        l.record_merge(1, 100, 99.0, 0.5)
        assert l.verify_balanced()[0]

    def test_pnl_with_merge(self):
        l = DoubleEntryLedger()
        l.record_buy_winning(1, 0.99, 100, is_maker=True)
        l.record_buy_loser(1, 0.005, 100)
        l.record_gas(1, 0.1)
        l.record_merge(1, 100, 99.0, 0.5)
        expected = (1.0 - 0.99) * 100 - 0.005 * 100 - 0.1
        assert abs(l.get_pnl() - expected) < 0.01

    def test_fee_recorded(self):
        l = DoubleEntryLedger()
        l.record_fee(1, 0.396, is_maker=False)
        assert l.verify_balanced()[0]
