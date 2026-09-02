"""Sweeper Bot V2 - Capital Recycler (Merge)"""
import time
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("sweeper.recycle")

@dataclass
class RecycleResult:
    condition_id: str
    success: bool
    shares_recycled: float
    usdc_recovered: float
    loser_cost: float
    net_gain: float
    is_paper: bool
    error: Optional[str] = None
    timestamp: float = 0.0

class CapitalRecycler:
    def __init__(self, config, order_builder, safety_rails):
        self.config = config
        self.builder = order_builder
        self.safety = safety_rails
        self._total_recycled = 0.0
        self._recycle_count = 0

    def recycle(self, detection_result, winning_shares: float) -> RecycleResult:
        if self.config.paper_mode:
            loser_cost = self.config.loser_max_price * winning_shares
            usdc_recovered = winning_shares
            net_gain = winning_shares - loser_cost - (winning_shares * self.config.buy_price)
            self._total_recycled += usdc_recovered
            self._recycle_count += 1
            return RecycleResult(
                condition_id=getattr(detection_result, 'condition_id', ''),
                success=True, shares_recycled=winning_shares,
                usdc_recovered=usdc_recovered, loser_cost=loser_cost,
                net_gain=net_gain, is_paper=True, timestamp=time.time(),
            )
        try:
            from py_clob_client_v2 import ClobClient
            client = self.builder._get_client()
            if not client:
                return RecycleResult(getattr(detection_result, 'condition_id', ''), False, 0, 0, 0, 0, False, "No client", time.time())
            loser_cost = self.config.loser_max_price * winning_shares
            neg_risk = getattr(detection_result, 'neg_risk', False)
            client.merge_positions(
                condition_id=getattr(detection_result, 'condition_id', ''),
                amount=winning_shares,
            )
            usdc_recovered = winning_shares
            net_gain = usdc_recovered - loser_cost - (winning_shares * self.config.buy_price)
            self._total_recycled += usdc_recovered
            self._recycle_count += 1
            return RecycleResult(getattr(detection_result, 'condition_id', ''), True, winning_shares, usdc_recovered, loser_cost, net_gain, False, None, time.time())
        except Exception as e:
            logger.error(f"Recycle failed: {e}")
            return RecycleResult(getattr(detection_result, 'condition_id', ''), False, 0, 0, 0, 0, False, str(e), time.time())

    def get_metrics(self) -> dict:
        return {'total_recycled': self._total_recycled, 'recycle_count': self._recycle_count}
