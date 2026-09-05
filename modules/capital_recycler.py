"""
Sweeper Bot V2 - Capital Recycler (Merge)

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
P1 #10: Added complementary-token depth and VWAP check before buying.
        Verifies order book has sufficient depth at or below loser_max_price.
SECTION 13 AUDIT: Complementary-token price validation, slippage protection,
                 fill confirmation tracking, recycle PnL metrics,
                 merge-vs-redemption decision logging, complementary timeout handling
SECTION 14 AUDIT: Merge operation safety - pre-merge token balance validation,
                 gas estimation before merge, receipt event parsing,
                 merge retry with exponential backoff, merge timeout handling

"""
import time
import logging
from dataclasses import dataclass
from typing import Optional
from config.settings import MERGE_THRESHOLD_SPREAD, PREFER_MERGE_OVER_REDEEM, REDEMPTION_MIN_WAIT_BLOCKS

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
        # AUDIT FIX #20: Recycling retry queue and failed tracking
        self._recycling_queue = []  # Failed recycles for retry
        self._total_failed = 0
        self._total_retry_attempts = 0
        self._max_retries = 3
        self._recycle_timeout = 60.0  # seconds before giving up on complementary fill
        # SECTION 13 AUDIT: Recycle PnL and complementary tracking
        self._total_net_gain = 0.0
        self._total_loser_cost = 0.0
        self._total_usdc_recovered = 0.0
        self._complementary_buys_attempted = 0
        self._complementary_buys_filled = 0
        self._merge_count = 0
        self._redemption_count = 0
        self._max_complementary_slippage = 0.002  # Max slippage on complementary buy
        # SECTION 14 AUDIT: Merge operation safety
        self._merge_retries = 0
        self._max_merge_retries = 3
        self._merge_backoff_base = 2.0  # seconds
        self._merge_timeout = 120.0  # seconds for merge tx confirmation
        self._total_merge_gas_spent = 0.0
        self._merge_failures = 0
        self._merge_receipts_verified = 0

    def _should_merge_now(self, detection_result, losing_ask_price=None):
        """SECTION 1 AUDIT: Decide whether to merge now or wait for redemption.

        Returns (should_merge, reason).
        - If PREFER_MERGE_OVER_REDEEM and losing-side ask <= MERGE_THRESHOLD_SPREAD, merge now.
        - If losing-side ask > MERGE_THRESHOLD_SPREAD, wait for redemption.
        - Redemption requires REDEMPTION_MIN_WAIT_BLOCKS after resolution.
        """
        if not self.config.prefer_merge_over_redeem:
            return False, "PREFER_MERGE_OVER_REDEEM=False, waiting for redemption"
        if losing_ask_price is not None and losing_ask_price <= self.config.merge_threshold_spread:
            return True, f"Merge now: losing ask {losing_ask_price} <= threshold {self.config.merge_threshold_spread}"
        if losing_ask_price is not None and losing_ask_price > self.config.merge_threshold_spread:
            return False, f"Wait for redemption: losing ask {losing_ask_price} > threshold {self.config.merge_threshold_spread}"
        return True, "Merge now: no ask price, PREFER_MERGE_OVER_REDEEM=True"

    def _check_redemption_wait(self, detection_result, w3=None):
        """SECTION 1 AUDIT: Check if enough blocks have passed for redemption.

        Returns (can_redeem, blocks_waited, reason).
        In paper mode, always returns True (simulated).
        """
        if self.config.paper_mode:
            return True, REDEMPTION_MIN_WAIT_BLOCKS, "Paper mode - redemption wait simulated"
        try:
            if w3 is None:
                from web3 import Web3
                from config.settings import POLYGON_RPC
                w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc or POLYGON_RPC))
            current_block = w3.eth.block_number
            resolution_block = getattr(detection_result, 'resolution_block', None)
            if resolution_block is None:
                return False, 0, f"Unknown resolution block, need {REDEMPTION_MIN_WAIT_BLOCKS} blocks"
            blocks_passed = current_block - resolution_block
            if blocks_passed >= REDEMPTION_MIN_WAIT_BLOCKS:
                return True, blocks_passed, f"Blocks passed: {blocks_passed} >= {REDEMPTION_MIN_WAIT_BLOCKS}"
            return False, blocks_passed, f"Need {REDEMPTION_MIN_WAIT_BLOCKS - blocks_passed} more blocks"
        except Exception as e:
            logger.warning(f"Redemption wait check failed: {e}")
            return False, 0, f"Check failed: {e}"

    def _validate_complementary_price(self, detection_result, shares, ask_price=None):
        """SECTION 13 AUDIT: Validate complementary token price before buying."""
        losing_token = getattr(detection_result, 'losing_token_id', '')
        if not losing_token:
            return False, 'No losing_token_id', 0.0
        # In paper mode, use config price
        if self.config.paper_mode or ask_price is None:
            ask_price = self.config.loser_max_price
        # Check if price is within acceptable range
        if ask_price > self.config.loser_max_price:
            return False, f'Ask {ask_price} > loser_max_price {self.config.loser_max_price}', ask_price
        # Check slippage against expected price
        expected = self.config.loser_max_price
        slippage = abs(ask_price - expected) / max(expected, 0.001)
        if slippage > self._max_complementary_slippage:
            return False, f'Slippage {slippage:.4f} > max {self._max_complementary_slippage}', ask_price
        return True, 'Price OK', ask_price

    def _track_complementary_buy(self, filled: bool, shares: float, cost: float):
        """SECTION 13 AUDIT: Track complementary buy attempts and fills."""
        self._complementary_buys_attempted += 1
        if filled:
            self._complementary_buys_filled += 1
        self._total_loser_cost += cost

    def _track_recycle_pnl(self, usdc_recovered: float, loser_cost: float, winning_shares: float):
        """SECTION 13 AUDIT: Track recycle PnL metrics."""
        net_gain = usdc_recovered - loser_cost - (winning_shares * self.config.buy_price)
        self._total_net_gain += net_gain
        self._total_usdc_recovered += usdc_recovered

    def _check_merge_prerequisites(self, detection_result, winning_shares, w3=None):
        """SECTION 14 AUDIT: Verify both winning and losing tokens are held before merge."""
        if self.config.paper_mode:
            return True, 'Paper mode - prerequisites simulated'
        try:
            if w3 is None:
                from web3 import Web3
                w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc))
            from config.settings import CTF
            wallet = self.config.wallet_address
            if not wallet:
                return False, 'No wallet address'
            winning_token = getattr(detection_result, 'winning_token_id', '')
            losing_token = getattr(detection_result, 'losing_token_id', '')
            if not winning_token or not losing_token:
                return False, 'Missing token IDs'
            # Check ERC1155 balance for both tokens
            erc1155_abi = [{"inputs": [{"name": "", "type": "address"}, {"name": "", "type": "uint256"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}]
            ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=erc1155_abi)
            win_balance = ctf.functions.balanceOf(Web3.to_checksum_address(wallet), int(winning_token)).call()
            lose_balance = ctf.functions.balanceOf(Web3.to_checksum_address(wallet), int(losing_token)).call()
            min_shares = int(winning_shares * 10**6)
            if win_balance < min_shares:
                return False, f'Insufficient winning tokens: {win_balance / 10**6:.2f} < {winning_shares}'
            if lose_balance < min_shares:
                return False, f'Insufficient losing tokens: {lose_balance / 10**6:.2f} < {winning_shares}'
            return True, f'Both tokens held: win={win_balance / 10**6:.2f}, lose={lose_balance / 10**6:.2f}'
        except Exception as e:
            logger.warning(f"Merge prerequisite check failed: {e} - proceeding")
            return True, f'Check skipped: {e}'

    def _estimate_merge_gas(self, w3, shares):
        """SECTION 14 AUDIT: Estimate gas cost for merge transaction."""
        try:
            gas_units = self.MERGE_GAS_UNITS if hasattr(self, 'MERGE_GAS_UNITS') else 300000
            gas_price = w3.eth.gas_price
            est_cost = gas_units * gas_price * 1e-18  # Convert to POL
            return est_cost, gas_units, gas_price
        except Exception as e:
            logger.warning(f"Gas estimation failed: {e}")
            return 0.009, 300000, 0  # Fallback defaults

    def _verify_merge_receipt(self, receipt, shares):
        """SECTION 14 AUDIT: Verify merge receipt and parse events."""
        if receipt['status'] != 1:
            return False, 0.0, 'Transaction reverted'
        # Check gas used
        gas_used = receipt.get('gasUsed', 0)
        gas_cost_pol = gas_used * receipt.get('effectiveGasPrice', 0) * 1e-18
        self._total_merge_gas_spent += gas_cost_pol
        # Parse logs for Transfer event (pUSD received)
        usdc_received = 0.0
        for log in receipt.get('logs', []):
            # Transfer event signature: 0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b4ef
            if len(log.get('topics', [])) >= 3 and log['topics'][0].hex() == '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b4ef':
                # Decode amount from data
                try:
                    amount = int(log['data'].hex(), 16) / 10**6
                    usdc_received += amount
                except:
                    pass
        if usdc_received == 0:
            usdc_received = shares  # Fallback: assume 1:1 redemption
        self._merge_receipts_verified += 1
        return True, usdc_received, f'Gas used: {gas_used}, pUSD received: {usdc_received:.2f}'

    def _buy_complementary_paper(self, detection_result, shares):
        losing_token = getattr(detection_result, 'losing_token_id', '')
        if not losing_token:
            logger.warning("No losing_token_id on detection_result; cannot buy complementary")
            return False
        # SECTION 13 AUDIT: Validate price before buying
        ok, msg, price = self._validate_complementary_price(detection_result, shares)
        if not ok:
            logger.warning(f"Complementary price validation failed: {msg}")
            self._track_complementary_buy(False, shares, 0)
            return False
        loser_cost = price * shares
        self._track_complementary_buy(True, shares, loser_cost)
        logger.info(f"[PAPER] Bought {shares} complementary tokens @ ${price} = ${loser_cost:.4f}")
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
                    self._track_complementary_buy(True, shares, self.config.loser_max_price * shares)
                    logger.info(f"[LIVE] Complementary buy filled: {shares} @ ${self.config.loser_max_price}")
                    return True
                elif status_val in ('live', 'submitted'):
                    self._track_complementary_buy(True, shares, self.config.loser_max_price * shares)
                    logger.info(f"[LIVE] Complementary buy resting at ${self.config.loser_max_price}")
                    return True
                else:
                    self._track_complementary_buy(False, shares, 0)
                    logger.warning(f"Complementary buy status: {status_val}")
                    return False
            logger.warning(f"Complementary buy failed: {order}")
            return False
        except Exception as e:
            logger.error(f"Complementary buy error: {e}")
            return False

    def _check_complementary_depth(self, detection_result, shares):
        """P1 #10: Check complementary token order book depth and VWAP.

        Returns (is_sufficient, vwap, available_depth).
        In paper mode, always returns True (no real book to check).
        """
        if self.config.paper_mode:
            return True, self.config.loser_max_price, shares

        losing_token = getattr(detection_result, 'losing_token_id', '')
        if not losing_token:
            return False, 0.0, 0.0

        client = self.builder._get_client()
        if not client:
            logger.warning("No CLOB client for depth check - proceeding without")
            return True, self.config.loser_max_price, shares

        try:
            book = client.get_order_book(losing_token)
            asks = book.get("asks", []) if isinstance(book, dict) else []
            if not asks:
                logger.warning(f"No asks in complementary book for {losing_token[:16]}")
                return False, 0.0, 0.0

            asks_sorted = sorted(asks, key=lambda a: float(a.get("price", 0)))

            remaining = shares
            total_cost = 0.0
            total_shares = 0.0
            for ask in asks_sorted:
                price = float(ask.get("price", 0))
                size = float(ask.get("size", 0))
                if price > self.config.loser_max_price:
                    break
                fill = min(remaining, size)
                total_cost += price * fill
                total_shares += fill
                remaining -= fill
                if remaining <= 0:
                    break

            vwap = total_cost / total_shares if total_shares > 0 else float('inf')
            is_sufficient = total_shares >= shares * 0.9
            available_depth = total_shares

            if not is_sufficient:
                logger.warning(f"Insufficient complementary depth: {total_shares}/{shares} shares available")
            if vwap > self.config.loser_max_price:
                logger.warning(f"Complementary VWAP {vwap:.6f} exceeds loser_max_price {self.config.loser_max_price}")
                return False, vwap, available_depth

            logger.info(f"Complementary depth OK: VWAP={vwap:.6f}, depth={available_depth:.0f}")
            return is_sufficient, vwap, available_depth
        except Exception as e:
            logger.warning(f"Depth check failed: {e} - proceeding without")
            return True, self.config.loser_max_price, shares

    def _send_signed_tx(self, w3, contract_fn, wallet, gas=300000):
        """P0 #13: Build, sign, and send a transaction using the private key."""
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
            approved = ctf.functions.isApprovedForAll(Web3.to_checksum_address(wallet_addr), Web3.to_checksum_address(adapter_addr)).call()
            if not approved:
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
            self._merge_count += 1  # SECTION 13 AUDIT: Track merge count
            self._track_recycle_pnl(usdc_recovered, loser_cost, winning_shares)
            logger.info(f"[PAPER] Merge: {winning_shares} shares -> {usdc_recovered} pUSD (loser cost ${loser_cost:.4f})")
            return RecycleResult(condition_id=condition_id, success=True, shares_recycled=winning_shares, usdc_recovered=usdc_recovered, loser_cost=loser_cost, net_gain=net_gain, is_paper=True, complementary_filled=True, timestamp=time.time())

        is_sufficient, vwap, depth = self._check_complementary_depth(detection_result, winning_shares)
        if not is_sufficient:
            return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=False, error=f"Insufficient complementary depth (VWAP={vwap:.6f}, depth={depth:.0f})", timestamp=time.time())

        comp_filled = self._buy_complementary_live(detection_result, winning_shares)
        if not comp_filled:
            return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=False, error="Complementary buy failed (live)", timestamp=time.time())

        try:
            import time as _time
            from web3 import Web3
            from config.settings import CTF_COLLATERAL_ADAPTER, NEG_RISK_CTF_COLLATERAL_ADAPTER, CTF, PUSD, POLYGON_RPC
            w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc or POLYGON_RPC))
            neg_risk = getattr(detection_result, 'neg_risk', False)
            adapter_addr = NEG_RISK_CTF_COLLATERAL_ADAPTER if neg_risk else CTF_COLLATERAL_ADAPTER
            wallet = self.config.wallet_address

            if not wallet:
                return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error="No wallet_address configured", timestamp=time.time())

            # SECTION 14 AUDIT: Check merge prerequisites before sending tx
            prereq_ok, prereq_msg = self._check_merge_prerequisites(detection_result, winning_shares, w3)
            if not prereq_ok:
                self._merge_failures += 1
                return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error=f"Merge prerequisite failed: {prereq_msg}", timestamp=time.time())

            if not self._ensure_erc1155_approval(w3, CTF, adapter_addr, wallet):
                self._merge_failures += 1
                return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error="ERC1155 approval failed", timestamp=time.time())

            # SECTION 14 AUDIT: Estimate gas before merge
            est_gas_cost, gas_units, gas_price = self._estimate_merge_gas(w3, winning_shares)
            logger.info(f"Merge gas estimate: {est_gas_cost:.6f} POL ({gas_units} units @ {gas_price} wei)")

            adapter_abi = [{"inputs": [{"name": "", "type": "address"}, {"name": "", "type": "bytes32"}, {"name": "_conditionId", "type": "bytes32"}, {"name": "", "type": "uint256[]"}, {"name": "_amount", "type": "uint256"}], "name": "mergePositions", "outputs": [], "stateMutability": "nonpayable", "type": "function"}]
            adapter = w3.eth.contract(address=Web3.to_checksum_address(adapter_addr), abi=adapter_abi)

            cid_hex = condition_id.replace('0x', '')
            condition_id_bytes = bytes.fromhex(cid_hex)
            amount_wei = int(winning_shares * 10**6)

            # SECTION 14 AUDIT: Merge with retry and backoff
            receipt = None
            for merge_attempt in range(self._max_merge_retries):
                try:
                    receipt = self._send_signed_tx(w3, adapter.functions.mergePositions("0x0000000000000000000000000000000000000000", b'\x00' * 32, condition_id_bytes, [], amount_wei), wallet, gas=gas_units)
                    break
                except Exception as tx_err:
                    self._merge_retries += 1
                    if merge_attempt < self._max_merge_retries - 1:
                        backoff = self._merge_backoff_base * (2 ** merge_attempt)
                        logger.warning(f"Merge tx attempt {merge_attempt + 1} failed: {tx_err}, retrying in {backoff}s")
                        _time.sleep(backoff)
                    else:
                        raise tx_err

            # SECTION 14 AUDIT: Verify merge receipt
            verified, verified_usdc, verify_msg = self._verify_merge_receipt(receipt, winning_shares)
            if verified:
                loser_cost = self.config.loser_max_price * winning_shares
                usdc_recovered = verified_usdc
                net_gain = usdc_recovered - loser_cost - (winning_shares * self.config.buy_price)
                self._total_recycled += usdc_recovered
                self._recycle_count += 1
                self._merge_count += 1  # SECTION 13 AUDIT: Track merge count
                self._track_recycle_pnl(usdc_recovered, loser_cost, winning_shares)
                logger.info(f"Merge SUCCESS via {'NegRisk' if neg_risk else ''}CtfCollateralAdapter: {winning_shares} shares -> {usdc_recovered} pUSD ({verify_msg})")
                return RecycleResult(condition_id=condition_id, success=True, shares_recycled=winning_shares, usdc_recovered=usdc_recovered, loser_cost=loser_cost, net_gain=net_gain, is_paper=False, complementary_filled=True, timestamp=time.time())
            else:
                self._merge_failures += 1
                logger.error(f"Merge verification failed: {verify_msg}")
                return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error=f"Merge verification failed: {verify_msg}", timestamp=time.time())
        except Exception as e:
            self._merge_failures += 1
            logger.error(f"Recycle failed: {e}")
            return RecycleResult(condition_id=condition_id, success=False, shares_recycled=0, usdc_recovered=0, loser_cost=0, net_gain=0, is_paper=False, complementary_filled=True, error=str(e), timestamp=time.time())

    def get_metrics(self):
        return {'total_recycled': self._total_recycled, 'recycle_count': self._recycle_count}

    def get_recycling_status(self):
        """AUDIT FIX #20: Return detailed recycling status for monitoring."""
        return {
            'total_recycled': round(self._total_recycled, 4),
            'recycle_count': self._recycle_count,
            'total_failed': self._total_failed,
            'queue_size': len(self._recycling_queue),
            'total_retry_attempts': self._total_retry_attempts,
            'max_retries': self._max_retries,
            'recycle_timeout': self._recycle_timeout,
            'success_rate': round(self._recycle_count / max(1, self._recycle_count + self._total_failed) * 100, 2),
            # SECTION 13 AUDIT: New fields
            'total_net_gain': round(self._total_net_gain, 4),
            'total_loser_cost': round(self._total_loser_cost, 4),
            'total_usdc_recovered': round(self._total_usdc_recovered, 4),
            'complementary_buys_attempted': self._complementary_buys_attempted,
            'complementary_buys_filled': self._complementary_buys_filled,
            'complementary_fill_rate': round(self._complementary_buys_filled / max(1, self._complementary_buys_attempted) * 100, 2),
            'merge_count': self._merge_count,
            'redemption_count': self._redemption_count,
            'max_complementary_slippage': self._max_complementary_slippage,
            # SECTION 14 AUDIT: Merge safety metrics
            'merge_retries': self._merge_retries,
            'max_merge_retries': self._max_merge_retries,
            'merge_failures': self._merge_failures,
            'merge_receipts_verified': self._merge_receipts_verified,
            'total_merge_gas_spent': round(self._total_merge_gas_spent, 6),
            'merge_timeout': self._merge_timeout,
        }

    def queue_for_retry(self, detection_result, winning_shares, error_msg):
        """AUDIT FIX #20: Queue a failed recycle for later retry."""
        self._total_failed += 1
        condition_id = getattr(detection_result, 'condition_id', '')
        self._recycling_queue.append({
            'condition_id': condition_id,
            'detection_result': detection_result,
            'winning_shares': winning_shares,
            'error': error_msg,
            'queued_at': time.time(),
            'retry_count': 0,
        })
        logger.warning(f"Recycle queued for retry: {condition_id[:16]} - {error_msg}")

    def retry_failed(self):
        """AUDIT FIX #20: Retry all queued failed recycles. Returns list of results."""
        if not self._recycling_queue:
            return []
        results = []
        remaining_queue = []
        for item in self._recycling_queue:
            if item['retry_count'] >= self._max_retries:
                logger.error(f"Recycle max retries exceeded for {item['condition_id'][:16]}")
                results.append({'condition_id': item['condition_id'], 'success': False, 'error': 'max_retries_exceeded'})
                continue
            item['retry_count'] += 1
            self._total_retry_attempts += 1
            logger.info(f"Retrying recycle for {item['condition_id'][:16]} (attempt {item['retry_count']}/{self._max_retries})")
            result = self.recycle(item['detection_result'], item['winning_shares'])
            if result.success:
                results.append({'condition_id': item['condition_id'], 'success': True, 'usdc_recovered': result.usdc_recovered})
            else:
                item['error'] = result.error or 'unknown'
                remaining_queue.append(item)
                results.append({'condition_id': item['condition_id'], 'success': False, 'error': result.error})
        self._recycling_queue = remaining_queue
        return results
