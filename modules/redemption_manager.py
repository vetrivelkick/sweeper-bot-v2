"""Sweeper Bot V2 - Redemption Manager

P0 #14: Post-resolution redemption of winning tokens for pUSD.
         Burns winning outcome tokens via CtfCollateralAdapter.redeemPositions
         after the market has resolved and payouts have been reported.

"""
import time
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("sweeper.redeem")


@dataclass
class RedeemResult:
    condition_id: str
    success: bool
    shares_redeemed: float
    usdc_recovered: float
    is_paper: bool
    error: Optional[str] = None
    timestamp: float = 0.0

class RedemptionManager:
    def __init__(self, config):
        self.config = config
        self._total_redeemed = 0.0
        self._redeem_count = 0
        self._approval_cache = set()

    def _ensure_erc1155_approval(self, w3, ctf_addr, adapter_addr, wallet_addr):
        if adapter_addr.lower() in self._approval_cache:
            return True
        try:
            from web3 import Web3
            erc1155_abi = [
                {"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}], "name": "setApprovalForAll", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
                {"inputs": [{"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}], "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"}
            ]
            ctf = w3.eth.contract(address=Web3.to_checksum_address(ctf_addr), abi=erc1155_abi)
            approved = ctf.functions.isApprovedForAll(Web3.to_checksum_address(wallet_addr), Web3.to_checksum_address(adapter_addr)).call()
            if not approved:
                tx = ctf.functions.setApprovalForAll(Web3.to_checksum_address(adapter_addr), True).transact({'from': wallet_addr})
                receipt = w3.eth.wait_for_transaction_receipt(tx)
                if receipt['status'] == 1:
                    logger.info(f"ERC1155 approval granted to adapter {adapter_addr}")
                else:
                    logger.error("ERC1155 approval tx reverted")
                    return False
            self._approval_cache.add(adapter_addr.lower())
            return True
        except Exception as e:
            logger.error(f"ERC1155 approval error: {e}")
            return False

    def redeem(self, detection_result, shares):
        condition_id = getattr(detection_result, 'condition_id', '')
        
        if self.config.paper_mode:
            usdc_recovered = shares
            self._total_redeemed += usdc_recovered
            self._redeem_count += 1
            logger.info(f"[PAPER] Redeemed {shares} winning tokens -> {usdc_recovered} pUSD")
            return RedeemResult(condition_id=condition_id, success=True, shares_redeemed=shares, usdc_recovered=usdc_recovered, is_paper=True, timestamp=time.time())
        
        try:
            from web3 import Web3
            from config.settings import CTF_COLLATERAL_ADAPTER, NEG_RISK_CTF_COLLATERAL_ADAPTER, CTF, PUSD, POLYGON_RPC
            w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc or POLYGON_RPC))
            neg_risk = getattr(detection_result, 'neg_risk', False)
            adapter_addr = NEG_RISK_CTF_COLLATERAL_ADAPTER if neg_risk else CTF_COLLATERAL_ADAPTER
            wallet = self.config.wallet_address
            
            if not wallet:
                return RedeemResult(condition_id=condition_id, success=False, shares_redeemed=0, usdc_recovered=0, is_paper=False, error="No wallet_address configured", timestamp=time.time())
            
            if not self._ensure_erc1155_approval(w3, CTF, adapter_addr, wallet):
                return RedeemResult(condition_id=condition_id, success=False, shares_redeemed=0, usdc_recovered=0, is_paper=False, error="ERC1155 approval failed", timestamp=time.time())
            
            adapter_abi = [{"inputs": [{"name": "", "type": "address"}, {"name": "", "type": "bytes32"}, {"name": "_conditionId", "type": "bytes32"}, {"name": "", "type": "uint256[]"}], "name": "redeemPositions", "outputs": [], "stateMutability": "nonpayable", "type": "function"}]
            adapter = w3.eth.contract(address=Web3.to_checksum_address(adapter_addr), abi=adapter_abi)
            
            cid_hex = condition_id.replace('0x', '')
            condition_id_bytes = bytes.fromhex(cid_hex)
            
            tx = adapter.functions.redeemPositions("0x0000000000000000000000000000000000000000", b'\x00' * 32, condition_id_bytes, [1, 2]).transact({'from': wallet, 'gas': 300000})
            receipt = w3.eth.wait_for_transaction_receipt(tx)
            if receipt['status'] == 1:
                usdc_recovered = shares
                self._total_redeemed += usdc_recovered
                self._redeem_count += 1
                logger.info(f"Redeem SUCCESS via {'NegRisk' if neg_risk else ''}CtfCollateralAdapter: {shares} tokens -> {usdc_recovered} pUSD")
                return RedeemResult(condition_id=condition_id, success=True, shares_redeemed=shares, usdc_recovered=usdc_recovered, is_paper=False, timestamp=time.time())
            else:
                logger.error(f"Redeem tx failed: {receipt}")
                return RedeemResult(condition_id=condition_id, success=False, shares_redeemed=0, usdc_recovered=0, is_paper=False, error="Redeem tx reverted", timestamp=time.time())
        except Exception as e:
            logger.error(f"Redeem failed: {e}")
            return RedeemResult(condition_id=condition_id, success=False, shares_redeemed=0, usdc_recovered=0, is_paper=False, error=str(e), timestamp=time.time())

    def get_metrics(self):
        return {'total_redeemed': self._total_redeemed, 'redeem_count': self._redeem_count}
