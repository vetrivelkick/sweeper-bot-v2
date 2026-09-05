"""SECTION 19 AUDIT: Unit tests for config/settings.py."""
import pytest
from config import (
    SweeperConfig, BotState, fee_per_share, get_fee_rate,
    GAS_PER_SHARE, BUY_PRICE, LOSER_MAX_PRICE, net_edge_per_share,
    DEFAULT_FEE_RATE
)
from config.settings import calculate_break_even, estimate_slippage


class TestFeeCalculations:
    def test_maker_fee_is_zero(self):
        assert fee_per_share(0.99, is_maker=True) == 0.0

    def test_taker_fee_positive(self):
        assert fee_per_share(0.99, is_maker=False) > 0

    def test_fee_rate_by_category(self):
        assert get_fee_rate("crypto") == 0.07
        assert get_fee_rate("sports") == 0.05
        assert get_fee_rate("geopolitics") == 0.00

    def test_fee_rate_unknown_uses_default(self):
        assert get_fee_rate("unknown") == DEFAULT_FEE_RATE


class TestNetEdge:
    def test_maker_edge_positive(self):
        assert net_edge_per_share(BUY_PRICE, LOSER_MAX_PRICE, is_maker=True) > 0

    def test_taker_edge_positive(self):
        assert net_edge_per_share(BUY_PRICE, LOSER_MAX_PRICE, is_maker=False) > 0


class TestSweeperConfig:
    def test_paper_mode_default(self):
        assert SweeperConfig().paper_mode is True

    def test_validate_returns_list(self):
        assert isinstance(SweeperConfig().validate(), list)

    def test_validate_passes_with_defaults(self):
        assert len(SweeperConfig().validate()) == 0

    def test_validate_fails_bad_buy_price(self):
        cfg = SweeperConfig()
        cfg.buy_price = 0.50
        assert any("buy_price" in e for e in cfg.validate())

    def test_section18_risk_params(self):
        cfg = SweeperConfig()
        assert cfg.circuit_breaker_cooldown == 300
        assert cfg.min_liquidity_usd == 100.0
        assert cfg.risk_score_degrade_threshold == 75.0
        assert cfg.max_position_size_pct == 0.25


class TestBotState:
    def test_save_and_load(self, tmp_path):
        s = BotState(total_buys=5, daily_pnl=1.5)
        p = str(tmp_path / "state.json")
        s.save(p)
        loaded = BotState.load(p)
        assert loaded.total_buys == 5
        assert loaded.daily_pnl == 1.5

    def test_state_version(self):
        assert BotState().state_version == 2

    def test_from_dict_preserves_429(self):
        s = BotState(rate_limit_429_count=7)
        assert BotState.from_dict(s.to_dict()).rate_limit_429_count == 7


class TestEconomicsFunctions:
    def test_break_even(self):
        be = calculate_break_even(0.99, is_maker=True)
        assert 0.99 < be < 1.0

    def test_slippage(self):
        assert 0 < estimate_slippage(100, 1000) < 0.05

    def test_slippage_zero_liquidity(self):
        assert estimate_slippage(100, 0) == float('inf')
