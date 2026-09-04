"""
Sweeper Bot V2 - Reconciliation Engine (Updated)

P0 #8: Added _create_position_from_fill() to populate open_positions
P0 #10: Added process_fills() to handle partial fills and create positions

"""
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
        # AUDIT FIX #21: Reconciliation metrics and history
        self._total_runs = 0
        self._total_phantoms_found = 0
        self._total_orders_filled = 0
        self._total_orders_expired = 0
        self._total_orders_cancelled = 0
        self._history = []  # Last N reconciliation results
        self._max_history = 50
        self._stale_position_threshold = 3600  # 1 hour

    def _create_position_from_fill(self, condition_id, tx_hash, shares, price, side):
        """P0 #8: Create a position entry from a confirmed fill."""
        position = {
            'condition_id': condition_id,
            'tx_hash': tx_hash,
            'shares': shares,
            'price': price,
            'side': side,
            'timestamp': time.time(),
        }
        self.safety.state.open_positions[condition_id] = position
        logger.info(f"Position created: {condition_id[:16]}... | {shares} shares @ ${price}")
        return position

    def process_fills(self, fills):
        """P0 #10: Process confirmed fills and create positions in open_positions."""
        for fill in fills:
            condition_id = getattr(fill, 'condition_id', '')
            tx_hash = getattr(fill, 'tx_hash', '')
            fill_amount = getattr(fill, 'fill_amount', getattr(fill, 'filled_shares', 0))
            price = getattr(fill, 'avg_fill_price', getattr(fill, 'price', 0))
            side = getattr(fill, 'side', 'BUY')
            if condition_id and tx_hash:
                self._create_position_from_fill(condition_id, tx_hash, fill_amount, price, side)

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
        self._total_runs += 1
        self._total_phantoms_found += phantom
        result = ReconciliationResult(total, real, phantom, phantoms_removed, self._last_run)
        self._history.append({'type': 'position', 'total': total, 'real': real, 'phantom': phantom, 'timestamp': self._last_run})
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
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
        self._total_orders_filled += len(result.filled)
        self._total_orders_expired += len(result.expired)
        self._total_orders_cancelled += len(result.cancelled)
        self._history.append({'type': 'order', 'filled': len(result.filled), 'expired': len(result.expired), 'cancelled': len(result.cancelled), 'resting': result.still_resting, 'timestamp': self._last_order_run})
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        if result.filled or result.expired:
            logger.info(f"Order reconciliation: {len(result.filled)} filled, {len(result.expired)} expired, {result.still_resting} resting")
        return result

    def should_run(self) -> bool:
        return time.time() - self._last_run >= 30

    def should_run_orders(self) -> bool:
        return time.time() - self._last_order_run >= self.config.order_reconcile_interval

    def last_run_time(self) -> float:
        return self._last_run

    def get_reconciliation_status(self) -> dict:
        """AUDIT FIX #21: Return detailed reconciliation status for monitoring."""
        return {
            'total_runs': self._total_runs,
            'total_phantoms_found': self._total_phantoms_found,
            'total_orders_filled': self._total_orders_filled,
            'total_orders_expired': self._total_orders_expired,
            'total_orders_cancelled': self._total_orders_cancelled,
            'history_size': len(self._history),
            'last_position_run': self._last_run,
            'last_order_run': self._last_order_run,
            'stale_threshold': self._stale_position_threshold,
        }

    def get_stale_positions(self) -> List[str]:
        """AUDIT FIX #21: Find positions older than stale threshold."""
        now = time.time()
        stale = []
        for condition_id, position in self.safety.state.open_positions.items():
            pos_time = position.get('timestamp', 0) if isinstance(position, dict) else 0
            if now - pos_time > self._stale_position_threshold:
                stale.append(condition_id)
        return stale

    def get_history(self, limit: int = 10) -> list:
        """AUDIT FIX #21: Return recent reconciliation history."""
        return self._history[-limit:]
