"""Sweeper Bot V2 - Capital Recycler (Merge)

FIX #6: Live mode merge via CtfCollateralAdapter on-chain (not client.merge_positions which doesn't exist in V2)
"""
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
        # FIX #6: Merge via CtfCollateralAdapter on-chain, not CLOB client
        # V2 SDK does NOT have client.merge_positions()
        # Must call CtfCollateralAdapter.mergePositions() or NegRiskCtfCollateralAdapter directly
        try:
            from web3 import Web3
            from config.settings import (
                CTF_COLLATERAL_ADAPTER, NEG_RISK_CTF_COLLATERAL_ADAPTER,
                CTF, PUSD, POLYGON_RPC
            )
            w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc or POLYGON_RPC))
            neg_risk = getattr(detection_result, 'neg_risk', False)
            adapter_addr = NEG_RISK_CTF_COLLATERAL_ADAPTER if neg_risk else CTF_COLLATERAL_ADAPTER
            adapter_abi = [{"inputs": [{"name": "conditionId", "type": "uint256"}, {"name": "positionIds", "type": "uint256[]"}, {"name": "amounts", "type": "uint256[]"}], "name": "mergePositions", "outputs": [], "stateMutability": "nonpayable"}]
            adapter = w3.eth.contract(address=Web3.to_checksum_address(adapter_addr), abi=adapter_abi)
            condition_id = getattr(detection_result, 'condition_id', '')
            winning_token = getattr(detection_result, 'winning_token_id', '')
            losing_token = getattr(detection_result, 'losing_token_id', '')
            amount_wei = w3.to_wei(winning_shares, 'ether')
            tx = adapter.functions.mergePositions(
                int(condition_id, 16),
                [int(winning_token, 16), int(losing_token, 16)],
                [amount_wei, amount_wei]
            ).transact({'from': self.config.wallet_address})
            receipt = w3.eth.wait_for_transaction_receipt(tx)
            if receipt['status'] == 1:
                loser_cost = self.config.loser_max_price * winning_shares
                usdc_recovered = winning_shares
                net_gain = usdc_recovered - loser_cost - (winning_shares * self.config.buy_price)
                self._total_recycled += usdc_recovered
                self._recycle_count += 1
                logger.info(f"Merge SUCCESS via {'NegRisk' if neg_risk else ''}CtfCollateralAdapter: {winning_shares} shares -> {usdc_recovered} pUSD")
                return RecycleResult(condition_id, True, winning_shares, usdc_recovered, loser_cost, net_gain, False, None, time.time())
            else:
                logger.error(f"Merge tx failed: {receipt}")
                return RecycleResult(condition_id, False, 0, 0, 0, 0, False, "Merge tx reverted", time.time())
        except Exception as e:
            logger.error(f"Recycle failed: {e}")
            return RecycleResult(getattr(detection_result, 'condition_id', ''), False, 0, 0, 0, 0, False, str(e), time.time())

    def get_metrics(self) -> dict:
        return {'total_recycled': self._total_recycled, 'recycle_count': self._recycle_count}
