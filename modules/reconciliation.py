"""Sweeper Bot V2 - Reconciliation Engine (Updated)"""
import time
import logging
from dataclasses import dataclass
from typing import List

logger = logging.getLogger("sweeper.reconcile")

@dataclass
class ReconciliationResult:
    total_positions: int
    real_positions: int
    phantom_positions: int
    phantoms_removed: List[str]
    timestamp: float

class OrderReconciliationResult:
    def __init__(self):
        self.filled = []
        self.expired = []
        self.cancelled = []
        self.still_resting = 0
        self.reserved_collateral = 0.0
        self.timestamp = time.time()

class ReconciliationEngine:
    def __init__(self, config, safety_rails, fill_confirmer, order_builder=None):
        self.config = config
        self.safety = safety_rails
        self.confirmer = fill_confirmer
        self.order_builder = order_builder
        self._last_run = 0
        self._last_order_run = 0

    def reconcile(self) -> ReconciliationResult:
        positions = self.safety.state.open_positions
        total = len(positions)
        real = 0; phantom = 0; phantoms_removed = []
        for condition_id in list(positions.keys()):
            position = positions[condition_id]
            result = self.confirmer.reconcile_position(position)
            if result == 'real': real += 1
            elif result == 'phantom':
                phantom += 1; phantoms_removed.append(condition_id)
                self.safety.record_ghost_fill(condition_id)
                logger.warning(f"Ghost fill removed: {condition_id}")
            else: real += 1
        self._last_run = time.time()
        result = ReconciliationResult(total, real, phantom, phantoms_removed, self._last_run)
        logger.info(f"Position reconciliation: {total} positions, {real} real, {phantom} phantoms")
        return result

    def reconcile_orders(self, ask_source=None) -> OrderReconciliationResult:
        result = OrderReconciliationResult()
        if not self.order_builder: return result
        order_result = self.order_builder.reconcile_orders(ask_source)
        result.filled = order_result.get("filled", [])
        result.expired = order_result.get("expired", [])
        result.cancelled = order_result.get("cancelled", [])
        result.still_resting = order_result.get("total_resting", 0)
        result.reserved_collateral = order_result.get("reserved_collateral", 0.0)
        self._last_order_run = time.time()
        if result.filled:
            logger.info(f"Order reconciliation: {len(result.filled)} filled, {len(result.expired)} expired, {result.still_resting} resting")
        return result

    def should_run(self) -> bool:
        return time.time() - self._last_run >= 30

    def should_run_orders(self) -> bool:
        return time.time() - self._last_order_run >= self.config.order_reconcile_interval

    def last_run_time(self) -> float:
        return self._last_run
