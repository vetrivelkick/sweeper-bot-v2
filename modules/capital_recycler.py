"""Sweeper Bot V2 - Capital Recycler (Merge)

P0 #12: Added complementary-token purchase before merge.
        Bot now buys the losing side at loser_max_price before merging.
P0 #13: Fixed merge ABI to match CtfCollateralAdapter interface.
        - conditionId encoded as bytes32 (was uint256)
        - mergePositions takes (address, bytes32, bytes32, uint256[], uint256)
        - Added ERC1155 setApprovalForAll before merge
        - Fixed NegRisk adapter selection
        - Fixed 6-decimal pUSD units (was using 18-decimal ether via to_wei)
        - Fixed unsigned .transact() to use signed transactions with private key
        - Fixed .static_call() to .call() for view functions
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
    complementary_filled: bool = False
    error: Optional[str] = None
    timestamp: float = 0.0


class CapitalRecycler:
    def __init__(self, config, order_builder, safety_rails):
        self.config = config
        self.builder = order_builder
        self.safety = safety_rails
        self._total_recycled = 0.0
        self._recycle_count = 0
        self._approval_cache = set()

    def _buy_complementary_paper(self, detection_result, shares):
        losing_token = getattr(detection_result, 'losing_token_id', '')
        if not losing_token:
            logger.warning("No losing_token_id on detection_result; cannot buy complementary")
            return False
        loser_cost = self.config.loser_max_price * shares
        logger.info(f"[PAPER] Bought {shares} complementary tokens @ ${self.config.loser_max_price} = ${loser_cost:.4f}")
        return True

    def _buy_complementary_live(self, detection_result, shares):
        losing_token = getattr(detection_result, 'losing_token_id', '')
        if not losing_token:
            logger.error("No losing_token_id on detection_result; cannot buy complementary")
            return False
        try:
            ok, order = self.builder.place_complementary_buy(detection_result, size=shares, tick_size=0.001)
            if ok and order:
                status = getattr(order, 'status', None)
                status_val = status.value if hasattr(status, 'value') else str(status)
                if status_val in ('filled', 'matched', 'paper'):
                    logger.info(f"[LIVE] Complementary buy filled: {shares} @ ${self.config.loser_max_price}")
                    return True
                elif status_val in ('live', 'submitted'):
                    logger.info(f"[LIVE] Complementary buy resting at ${self.config.loser_max_price}")
                    return True
                else:
                    logger.warning(f"Complementary buy status: {status_val}")
                    return False
            logger.warning(f"Complementary buy failed: {order}")
            return False
        except Exception as e:
            logger.error(f"Complementary buy error: {e}")
            return False

    def _send_signed_tx(self, w3, contract_fn, wallet, gas=300000):
        """P0 #13: Build, sign, and send a transaction using the private key.
        
        Replaces unsigned .transact({'from': wallet}) calls which only work
        for nodes with unlocked accounts (e.g., Ganache). Production Polygon
        requires signed transactions with the private key.
        """
        from eth_account import Account
        from web3 import Web3
        nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(wallet))
        tx = contract_fn.build_transaction({
            'from': wallet,
            'gas': gas,
            'nonce': nonce,
            'chainId': 137,
            'gasPrice': w3.eth.gas_price,
        })
        signed = Account.sign_transaction(tx, self.config.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        return w3.eth.wait_for_transaction_receipt(tx_hash)

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
            # P0 #13: Use .call() instead of .static_call() for view functions
            approved = ctf.functions.isApprovedForAll(Web3.to_checksum_address(wallet_addr), Web3.to_checksum_address(adapter_addr)).call()
            if not approved:
                # P0 #13: Use signed transaction instead of unsigned .transact()
                receipt = self._send_signed_tx(w3, ctf.functions.setApprovalForAll(Web3.to_checksum_address(adapter_addr), True), wallet_addr, gas=200000)
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

    def recycle(self, detection_result, winning_shares):
        condition_id = getattr(detection_result, 'condition_id', '')
        
        if self.config.paper_mode:
            comp_filled = self._buy_complementary_paper(detection_result, winning_shares)
            if not comp_filled:
                return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=True, complementary_filled=False, error="Complementary buy failed (paper)", timestamp=time.time())
            loser_cost = self.config.loser_max_price * winning_shares
            usdc_recovered = winning_shares
            net_gain = usdc_recovered - loser_cost - (winning_shares * self.config.buy_price)
            self._total_recycled += usdc_recovered
            self._recycle_count += 1
            logger.info(f"[PAPER] Merge: {winning_shares} shares -> {usdc_recovered} pUSD (loser cost ${loser_cost:.4f})")
            return RecycleResult(condition_id=condition_id, success=True, shares_recycled=winning_shares, usdc_recovered=usdc_recovered, loser_cost=loser_cost, net_gain=net_gain, is_paper=True, complementary_filled=True, timestamp=time.time())
        
        comp_filled = self._buy_complementary_live(detection_result, winning_shares)
        if not comp_filled:
            return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=False, error="Complementary buy failed (live)", timestamp=time.time())
        
        try:
            from web3 import Web3
            from config.settings import CTF_COLLATERAL_ADAPTER, NEG_RISK_CTF_COLLATERAL_ADAPTER, CTF, PUSD, POLYGON_RPC
            w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc or POLYGON_RPC))
            neg_risk = getattr(detection_result, 'neg_risk', False)
            adapter_addr = NEG_RISK_CTF_COLLATERAL_ADAPTER if neg_risk else CTF_COLLATERAL_ADAPTER
            wallet = self.config.wallet_address
            
            if not wallet:
                return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error="No wallet_address configured", timestamp=time.time())
            
            if not self._ensure_erc1155_approval(w3, CTF, adapter_addr, wallet):
                return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error="ERC1155 approval failed", timestamp=time.time())
            
            adapter_abi = [{"inputs": [{"name": "", "type": "address"}, {"name": "", "type": "bytes32"}, {"name": "_conditionId", "type": "bytes32"}, {"name": "", "type": "uint256[]"}, {"name": "_amount", "type": "uint256"}], "name": "mergePositions", "outputs": [], "stateMutability": "nonpayable", "type": "function"}]
            adapter = w3.eth.contract(address=Web3.to_checksum_address(adapter_addr), abi=adapter_abi)
            
            cid_hex = condition_id.replace('0x', '')
            condition_id_bytes = bytes.fromhex(cid_hex)
            # P0 #13: pUSD has 6 decimals, NOT 18 (ether). Using to_wei() overstates amount by 10^12.
            amount_wei = int(winning_shares * 10**6)
            
            # P0 #13: Use signed transaction instead of unsigned .transact()
            receipt = self._send_signed_tx(w3, adapter.functions.mergePositions("0x0000000000000000000000000000000000000000", b'\x00' * 32, condition_id_bytes, [], amount_wei), wallet, gas=300000)
            if receipt['status'] == 1:
                loser_cost = self.config.loser_max_price * winning_shares
                usdc_recovered = winning_shares
                net_gain = usdc_recovered - loser_cost - (winning_shares * self.config.buy_price)
                self._total_recycled += usdc_recovered
                self._recycle_count += 1
                logger.info(f"Merge SUCCESS via {'NegRisk' if neg_risk else ''}CtfCollateralAdapter: {winning_shares} shares -> {usdc_recovered} pUSD")
                return RecycleResult(condition_id=condition_id, success=True, shares_recycled=winning_shares, usdc_recovered=usdc_recovered, loser_cost=loser_cost, net_gain=net_gain, is_paper=False, complementary_filled=True, timestamp=time.time())
            else:
                logger.error(f"Merge tx failed: {receipt}")
                return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error="Merge tx reverted", timestamp=time.time())
        except Exception as e:
            logger.error(f"Recycle failed: {e}")
            return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error=str(e), timestamp=time.time())

    def get_metrics(self):
        return {'total_recycled': self._total_recycled, 'recycle_count': self._recycle_count}
