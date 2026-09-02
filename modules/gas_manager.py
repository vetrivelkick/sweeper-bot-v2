"""Sweeper Bot V2 - Gas Manager"""
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

class GasManager:
    def __init__(self, config, safety_rails):
        self.config = config
        self.safety = safety_rails
        self._w3 = None
        self._balance = 0.0
        self._last_check = 0
        self._failed_claims_queue = []

    def _get_web3(self):
        if self._w3 or self.config.paper_mode:
            return self._w3
        try:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc))
        except Exception as e:
            logger.error(f"Web3 init failed: {e}")
        return self._w3

    def check_balance(self) -> GasStatus:
        if self.config.paper_mode:
            self._balance = 10.0
        else:
            w3 = self._get_web3()
            if w3 and getattr(self.config, 'wallet_address', ''):
                try:
                    balance_wei = w3.eth.get_balance(self.config.wallet_address)
                    self._balance = w3.from_wei(balance_wei, 'ether')
                except Exception as e:
                    logger.error(f"Balance check failed: {e}")
        self._last_check = time.time()
        is_low = self._balance < self.config.gas_floor * 2
        is_critical = self._balance < self.config.gas_floor
        return GasStatus(
            balance_pol=self._balance,
            floor=self.config.gas_floor,
            is_low=is_low,
            is_critical=is_critical,
            failed_claims=len(self._failed_claims_queue),
            last_check=self._last_check,
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
        return {'balance': gs.balance_pol, 'floor': gs.floor, 'is_low': gs.is_low, 'is_critical': gs.is_critical}
