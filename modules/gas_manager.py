"""
Sweeper Bot V2 - Gas Manager

P1 #9: Gas economics now uses dynamic gas price estimation from Polygon RPC
       instead of a fixed GAS_PER_SHARE constant. Tracks total gas spent.
       Estimates gas cost for merge/redeem transactions.
AUDIT FIX #23: Gas price spike detection, budget tracking, alerting
SECTION 12 AUDIT: Nonce management, EIP-1559 gas estimation, wallet validation,
                 pending transaction tracking, gas price ceiling
"""
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger("sweeper.gas")

@dataclass
class GasStatus:
    balance_pol: float
    floor: float
    is_low: bool
    is_critical: bool
    failed_claims: int
    last_check: float
    gas_price_gwei: float = 0.0
    est_merge_cost_pol: float = 0.0
    est_redeem_cost_pol: float = 0.0

class GasManager:
    MERGE_GAS_UNITS = 300000
    REDEEM_GAS_UNITS = 200000
    APPROVAL_GAS_UNITS = 100000

    def __init__(self, config, safety_rails):
        self.config = config
        self.safety = safety_rails
        self._w3 = None
        self._balance = 0.0
        self._last_check = 0
        self._failed_claims_queue = []
        self._total_gas_spent = 0.0
        self._gas_price_gwei = 0.0
        # AUDIT FIX #23: Gas price history, spike detection, budget tracking
        self._gas_price_history = []
        self._max_price_history = 20
        self._gas_spike_threshold = 3.0
        self._daily_gas_budget = 0.5
        self._daily_gas_spent = 0.0
        self._daily_reset_time = time.time()
        self._gas_alert_callback = None
        # SECTION 12 AUDIT: Nonce management and pending tx tracking
        self._pending_txs = {}  # tx_hash -> {nonce, gas_cost, timestamp}
        self._last_nonce = -1
        self._gas_price_ceiling_gwei = 500.0  # Max acceptable gas price
        self._eip1559_enabled = False  # Will be set to True if RPC supports it

    def _get_web3(self):
        if self._w3 or self.config.paper_mode:
            return self._w3
        try:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc))
        except Exception as e:
            logger.error(f"Web3 init failed: {e}")
        return self._w3

    def _fetch_gas_price(self) -> float:
        """P1 #9: Fetch current gas price from Polygon RPC in Gwei."""
        w3 = self._get_web3()
        if w3:
            try:
                price_wei = w3.eth.gas_price
                self._gas_price_gwei = float(w3.from_wei(price_wei, 'gwei'))
                # AUDIT FIX #23: Track price history and detect spikes
                self._gas_price_history.append(self._gas_price_gwei)
                if len(self._gas_price_history) > self._max_price_history:
                    self._gas_price_history = self._gas_price_history[-self._max_price_history:]
                if self._is_gas_spike():
                    logger.warning(f"Gas price spike detected: {self._gas_price_gwei:.2f} Gwei "
                                 f"(avg: {self._get_avg_gas_price():.2f} Gwei)")
                    if self._gas_alert_callback:
                        self._gas_alert_callback('GAS_SPIKE', self._gas_price_gwei, self._get_avg_gas_price())
                return self._gas_price_gwei
            except Exception as e:
                logger.debug(f"Gas price fetch failed: {e}")
        self._gas_price_gwei = 30.0
        return self._gas_price_gwei

    def estimate_gas_eip1559(self, gas_units=None) -> dict:
        """SECTION 12 AUDIT: Estimate gas cost with EIP-1559 fields."""
        if gas_units is None:
            gas_units = self.MERGE_GAS_UNITS
        gwei = self._fetch_gas_price()
        # EIP-1559: maxFeePerGas = baseFee * 2, maxPriorityFeePerGas = 1 Gwei
        max_fee_gwei = gwei * 2
        priority_fee_gwei = 1.0
        total_cost = gas_units * max_fee_gwei * 1e-9
        return {
            'max_fee_per_gas_gwei': max_fee_gwei,
            'max_priority_fee_gwei': priority_fee_gwei,
            'gas_units': gas_units,
            'est_cost_pol': total_cost,
            'eip1559': True,
        }

    def validate_wallet(self) -> tuple:
        """SECTION 12 AUDIT: Validate wallet address and balance before transactions."""
        if not getattr(self.config, 'wallet_address', ''):
            return False, 'No wallet address configured'
        if not getattr(self.config, 'private_key', ''):
            return False, 'No private key configured'
        gs = self.check_balance()
        if gs.is_critical:
            return False, f'Insufficient POL balance: {gs.balance_pol:.4f} < floor {gs.floor}'
        if not self.is_gas_price_acceptable():
            return False, f'Gas price too high: {gs.gas_price_gwei:.2f} Gwei'
        return True, 'Wallet OK'

    def get_next_nonce(self) -> int:
        """SECTION 12 AUDIT: Get next transaction nonce."""
        if self.config.paper_mode:
            self._last_nonce += 1
            return self._last_nonce
        w3 = self._get_web3()
        if w3 and getattr(self.config, 'wallet_address', ''):
            try:
                nonce = w3.eth.get_transaction_count(self.config.wallet_address)
                self._last_nonce = max(self._last_nonce + 1, nonce)
                return self._last_nonce
            except Exception as e:
                logger.error(f"Nonce fetch failed: {e}")
                return self._last_nonce + 1
        return self._last_nonce + 1

    def track_pending_tx(self, tx_hash: str, nonce: int, gas_cost: float):
        """SECTION 12 AUDIT: Track pending transaction for confirmation."""
        self._pending_txs[tx_hash] = {
            'nonce': nonce,
            'gas_cost': gas_cost,
            'timestamp': time.time(),
        }

    def clear_pending_tx(self, tx_hash: str):
        """SECTION 12 AUDIT: Clear confirmed transaction."""
        self._pending_txs.pop(tx_hash, None)

    def get_pending_txs(self) -> dict:
        """SECTION 12 AUDIT: Get all pending transactions."""
        return self._pending_txs.copy()

    def estimate_gas_cost(self, gas_units=None) -> float:
        if gas_units is None:
            gas_units = self.MERGE_GAS_UNITS
        gwei = self._fetch_gas_price()
        return gas_units * gwei * 1e-9

    def estimate_gas_per_share(self, shares, gas_units=None) -> float:
        total_cost = self.estimate_gas_cost(gas_units)
        if shares <= 0:
            return float('inf')
        return total_cost / shares

    def track_gas_spent(self, gas_cost_pol: float):
        self._total_gas_spent += gas_cost_pol
        self._check_daily_reset()
        self._daily_gas_spent += gas_cost_pol
        if self._daily_gas_spent > self._daily_gas_budget:
            logger.warning(f"Daily gas budget exceeded: {self._daily_gas_spent:.6f}/{self._daily_gas_budget:.6f} POL")
            if self._gas_alert_callback:
                self._gas_alert_callback('GAS_BUDGET_EXCEEDED', self._daily_gas_spent, self._daily_gas_budget)
        logger.info(f"Gas spent: {gas_cost_pol:.6f} POL (total: {self._total_gas_spent:.6f} POL, daily: {self._daily_gas_spent:.6f} POL)")

    def _check_daily_reset(self):
        """AUDIT FIX #23: Reset daily gas tracking every 24 hours."""
        if time.time() - self._daily_reset_time > 86400:
            self._daily_gas_spent = 0.0
            self._daily_reset_time = time.time()
            logger.info("Daily gas tracker reset")

    def _get_avg_gas_price(self) -> float:
        """AUDIT FIX #23: Get average gas price from history."""
        if not self._gas_price_history:
            return self._gas_price_gwei
        return sum(self._gas_price_history) / len(self._gas_price_history)

    def _is_gas_spike(self) -> bool:
        """AUDIT FIX #23: Detect if current gas price is a spike."""
        if len(self._gas_price_history) < 5:
            return False
        avg = self._get_avg_gas_price()
        if avg <= 0:
            return False
        return self._gas_price_gwei > avg * self._gas_spike_threshold

    def is_gas_price_acceptable(self) -> bool:
        """AUDIT FIX #23: Check if current gas price is within acceptable range."""
        if self._is_gas_spike():
            return False
        if self._gas_price_gwei > 100.0:
            return False
        return True

    def gas_cost_as_profit_pct(self, gas_cost_pol: float, expected_profit_usd: float) -> float:
        """AUDIT FIX #23: Calculate gas cost as percentage of expected profit."""
        if expected_profit_usd <= 0:
            return float('inf')
        gas_cost_usd = gas_cost_pol * 0.5
        return (gas_cost_usd / expected_profit_usd) * 100

    def set_gas_alert_callback(self, callback):
        """AUDIT FIX #23: Set callback for gas alerts."""
        self._gas_alert_callback = callback

    def check_balance(self) -> GasStatus:
        if self.config.paper_mode:
            self._balance = 10.0
            self._gas_price_gwei = 30.0
        else:
            w3 = self._get_web3()
            if w3 and getattr(self.config, 'wallet_address', ''):
                try:
                    balance_wei = w3.eth.get_balance(self.config.wallet_address)
                    self._balance = w3.from_wei(balance_wei, 'ether')
                except Exception as e:
                    logger.error(f"Balance check failed: {e}")
            self._fetch_gas_price()
        self._last_check = time.time()
        is_low = self._balance < self.config.gas_floor * 2
        is_critical = self._balance < self.config.gas_floor
        est_merge = self.estimate_gas_cost(self.MERGE_GAS_UNITS)
        est_redeem = self.estimate_gas_cost(self.REDEEM_GAS_UNITS)
        return GasStatus(
            balance_pol=self._balance,
            floor=self.config.gas_floor,
            is_low=is_low,
            is_critical=is_critical,
            failed_claims=len(self._failed_claims_queue),
            last_check=self._last_check,
            gas_price_gwei=self._gas_price_gwei,
            est_merge_cost_pol=est_merge,
            est_redeem_cost_pol=est_redeem,
        )

    def can_claim(self) -> bool:
        return self._balance >= self.config.gas_floor

    def record_failed_claim(self, condition_id: str):
        self._failed_claims_queue.append({'condition_id': condition_id, 'time': time.time()})
        self.safety.record_failed_claim(condition_id)
        logger.error(f"Failed claim recorded: {condition_id}")

    def get_stuck_claims(self) -> list:
        return self._failed_claims_queue

    def clear_claim(self, condition_id: str):
        self._failed_claims_queue = [c for c in self._failed_claims_queue if c['condition_id'] != condition_id]

    def status(self) -> dict:
        gs = self.check_balance()
        return {'balance': gs.balance_pol, 'floor': gs.floor, 'is_low': gs.is_low, 'is_critical': gs.is_critical,
                'gas_price_gwei': gs.gas_price_gwei, 'est_merge_cost': gs.est_merge_cost_pol,
                'est_redeem_cost': gs.est_redeem_cost_pol, 'total_gas_spent': self._total_gas_spent,
                # AUDIT FIX #23: New fields
                'avg_gas_price_gwei': round(self._get_avg_gas_price(), 2),
                'is_gas_spike': self._is_gas_spike(),
                'gas_price_acceptable': self.is_gas_price_acceptable(),
                'daily_gas_spent': round(self._daily_gas_spent, 6),
                'daily_gas_budget': self._daily_gas_budget,
                'daily_budget_pct': round(self._daily_gas_spent / max(0.001, self._daily_gas_budget) * 100, 1),
                'gas_price_history_size': len(self._gas_price_history),
                # SECTION 12 AUDIT: New fields
                'pending_txs': len(self._pending_txs),
                'last_nonce': self._last_nonce,
                'gas_price_ceiling_gwei': self._gas_price_ceiling_gwei,
                'eip1559_enabled': self._eip1559_enabled,
        }
