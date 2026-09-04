"""Sweeper Bot V2 - Gas Manager

P1 #9: Gas economics now uses dynamic gas price estimation from Polygon RPC
       instead of a fixed GAS_PER_SHARE constant. Tracks total gas spent.
       Estimates gas cost for merge/redeem transactions.
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
    # P1 #9: Typical gas units for Polygon transactions
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
        self._total_gas_spent = 0.0  # P1 #9: Track total gas spent
        self._gas_price_gwei = 0.0  # P1 #9: Cached gas price

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
                return self._gas_price_gwei
            except Exception as e:
                logger.debug(f"Gas price fetch failed: {e}")
        # Fallback: 30 Gwei typical for Polygon
        self._gas_price_gwei = 30.0
        return self._gas_price_gwei

    def estimate_gas_cost(self, gas_units=None) -> float:
        """P1 #9: Estimate gas cost in POL for a transaction.

        Uses current gas price from Polygon RPC.
        Default: merge transaction (~300k gas units).
        """
        if gas_units is None:
            gas_units = self.MERGE_GAS_UNITS
        gwei = self._fetch_gas_price()
        # gas_cost_pol = gas_units * gas_price_gwei * 1e-9
        return gas_units * gwei * 1e-9

    def estimate_gas_per_share(self, shares, gas_units=None) -> float:
        """P1 #9: Calculate gas cost per share for a merge/redeem transaction."""
        total_cost = self.estimate_gas_cost(gas_units)
        if shares <= 0:
            return float('inf')
        return total_cost / shares

    def track_gas_spent(self, gas_cost_pol: float):
        """P1 #9: Track actual gas spent on transactions."""
        self._total_gas_spent += gas_cost_pol
        logger.info(f"Gas spent: {gas_cost_pol:.6f} POL (total: {self._total_gas_spent:.6f} POL)")

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
        # P1 #9: Calculate estimated transaction costs
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
                'est_redeem_cost': gs.est_redeem_cost_pol, 'total_gas_spent': self._total_gas_spent}
