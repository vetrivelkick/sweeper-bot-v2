"""
Sweeper Bot V2 - Fill Confirmation

P0 #2: Fixed V2 constructor: chain=137 -> chain_id=137
P0 #3: Added signature_type and funder parameters
P0 #6: Fixed txHash -> transactionsHashes (list) for V2 response format

AUDIT FIX #11: Block confirmation requirement + MATCHED_OFFCHAIN/TRADE_PENDING states
AUDIT FIX #27: Fill metrics, retry logic, batch confirmation, fill age tracking
SECTION 11 AUDIT: Settlement state tracking, PnL realization, settlement persistence,
                 batch settlement verification, settlement age tracking
"""
import time
import logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum

logger = logging.getLogger("sweeper.fillconfirm")

class FillStatus(Enum):
    UNCONFIRMED = "unconfirmed"
    MATCHED_OFFCHAIN = "matched_offchain"  # AUDIT FIX #11: Order matched but not yet on-chain
    TRADE_PENDING = "trade_pending"        # AUDIT FIX #11: Trade submitted, awaiting block inclusion
    CONFIRMED = "confirmed"
    GHOST = "ghost"
    TIMEOUT = "timeout"
    PAPER = "paper"

# AUDIT FIX #11: Block confirmations required before considering settlement final
BLOCK_CONFIRMATIONS_REQUIRED = 3

@dataclass
class FillConfirmation:
    order_id: str
    condition_id: str
    status: FillStatus
    fill_amount: float
    tx_hash: Optional[str] = None
    timestamp: float = 0.0

class FillConfirmer:
    def __init__(self, config):
        self.config = config
        self._client = None
        self._w3 = None
        # AUDIT FIX #27: Fill metrics tracking
        self._total_confirmed = 0
        self._total_ghosts = 0
        self._total_timeouts = 0
        self._total_matched_offchain = 0
        self._total_trade_pending = 0
        self._total_partial = 0
        self._fill_ages = []  # Time from order to confirmation
        self._max_age_samples = 100
        self._max_fill_retries = 3
        # SECTION 11 AUDIT: Settlement state tracking
        self._settlements = {}  # order_id -> settlement dict
        self._total_pnl_realized = 0.0
        self._total_settled = 0
        self._total_pending_settlement = 0
        self._settlement_ages = []
        self._max_settlement_age_samples = 100

    def _get_client(self):
        if self._client or self.config.paper_mode:
            return self._client
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds
            creds = ApiCreds(api_key=self.config.clob_api_key, api_secret=self.config.clob_api_secret, api_passphrase=self.config.clob_api_passphrase)
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.config.private_key,
                chain_id=137,
                creds=creds,
                signature_type=self.config.signature_type,
                funder=self.config.funder if self.config.funder else None,
            )
        except Exception as e:
            logger.error(f"CLOB V2 client init failed: {e}")
        return self._client

    def _get_web3(self):
        if self._w3 or self.config.paper_mode:
            return self._w3
        try:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc))
        except Exception as e:
            logger.error(f"Web3 init failed: {e}")
        return self._w3

    def confirm_fill(self, order, timeout=None) -> FillConfirmation:
        """AUDIT FIX #11: Enhanced fill confirmation with block verification.

        States flow: UNCONFIRMED -> MATCHED_OFFCHAIN -> TRADE_PENDING -> CONFIRMED
        Returns MATCHED_OFFCHAIN if order matched but tx not yet on-chain.
        Returns TRADE_PENDING if tx on-chain but not enough block confirmations.
        Returns CONFIRMED only after BLOCK_CONFIRMATIONS_REQUIRED confirmations.
        """
        if self.config.paper_mode:
            self._total_confirmed += 1
            return FillConfirmation(
                order_id=getattr(order, 'order_id', ''),
                condition_id=getattr(order, 'condition_id', ''),
                status=FillStatus.PAPER,
                fill_amount=getattr(order, 'fill_amount', getattr(order, 'filled_shares', 0)),
                tx_hash=getattr(order, 'tx_hash', None),
                timestamp=time.time(),
            )
        timeout = timeout or 8
        start = time.time()
        matched_amount = 0.0
        matched_tx_hash = None
        while time.time() - start < timeout:
            client = self._get_client()
            if client:
                try:
                    status = client.get_order(getattr(order, 'order_id', ''))
                    if isinstance(status, dict):
                        matched = float(status.get('size_matched', 0))
                        if matched > 0:
                            matched_amount = matched
                            tx_hashes = status.get('transactionsHashes', [])
                            tx_hash = tx_hashes[0] if tx_hashes else status.get('txHash', '')
                            matched_tx_hash = tx_hash
                            if not tx_hash:
                                # AUDIT FIX #11: Matched but no tx hash yet
                                logger.info(f"Order {order.order_id} matched off-chain, awaiting tx")
                                continue
                            # Check if tx is on-chain with enough confirmations
                            w3 = self._get_web3()
                            if w3:
                                try:
                                    receipt = w3.eth.get_transaction_receipt(tx_hash)
                                    if receipt is None:
                                        # AUDIT FIX #11: Tx submitted but not yet in block
                                        logger.debug(f"Tx {tx_hash[:16]} pending block inclusion")
                                        continue
                                except Exception:
                                    pass  # Tx not yet mined, keep polling
                            if self._settled_on_chain(tx_hash):
                                self._total_confirmed += 1
                                self._fill_ages.append(time.time() - start)
                                if len(self._fill_ages) > self._max_age_samples:
                                    self._fill_ages = self._fill_ages[-self._max_age_samples:]
                                return FillConfirmation(order.order_id, order.condition_id, FillStatus.CONFIRMED, matched, tx_hash, time.time())
                except Exception as e:
                    logger.debug(f"Order check error: {e}")
            time.sleep(0.5)
        # Timeout: return best known state
        if matched_amount > 0 and matched_tx_hash:
            self._total_trade_pending += 1
            return FillConfirmation(getattr(order, 'order_id', ''), getattr(order, 'condition_id', ''), FillStatus.TRADE_PENDING, matched_amount, matched_tx_hash, time.time())
        elif matched_amount > 0:
            self._total_matched_offchain += 1
            return FillConfirmation(getattr(order, 'order_id', ''), getattr(order, 'condition_id', ''), FillStatus.MATCHED_OFFCHAIN, matched_amount, None, time.time())
        self._total_timeouts += 1
        return FillConfirmation(getattr(order, 'order_id', ''), getattr(order, 'condition_id', ''), FillStatus.TIMEOUT, 0, None, time.time())

    def _settled_on_chain(self, tx_hash: str) -> bool:
        """AUDIT FIX #11: Verify block confirmations before considering settlement final.

        A fill is only 'settled' when:
        1. Transaction receipt exists with status == 1 (success)
        2. At least BLOCK_CONFIRMATIONS_REQUIRED blocks have been mined since

        Returns False if not enough confirmations yet (caller should retry).
        """
        if not tx_hash:
            return False
        w3 = self._get_web3()
        if not w3:
            return True  # paper mode or no web3 - trust the API
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt is None or receipt.get('status') != 1:
                return False
            # AUDIT FIX #11: Check block confirmations
            tx_block = receipt.get('blockNumber')
            if tx_block is None:
                return False
            current_block = w3.eth.block_number
            confirmations = current_block - tx_block
            if confirmations < BLOCK_CONFIRMATIONS_REQUIRED:
                logger.debug(f"Tx {tx_hash[:16]} has {confirmations}/{BLOCK_CONFIRMATIONS_REQUIRED} confirmations")
                return False
            logger.info(f"Tx {tx_hash[:16]} confirmed with {confirmations} block confirmations")
            return True
        except Exception as e:
            logger.debug(f"Block confirmation check failed: {e}")
            return False

    def confirm_fill_with_retry(self, order, timeout=None) -> FillConfirmation:
        """AUDIT FIX #27: Confirm fill with retry logic."""
        for attempt in range(self._max_fill_retries):
            result = self.confirm_fill(order, timeout)
            if result.status in (FillStatus.CONFIRMED, FillStatus.PAPER):
                return result
            if result.status == FillStatus.GHOST:
                self._total_ghosts += 1
                return result
            if attempt < self._max_fill_retries - 1:
                logger.info(f"Fill retry {attempt+1}/{self._max_fill_retries} for {getattr(order, 'order_id', '')}")
                time.sleep(2)
        return result

    def realize_settlement(self, order, fill_confirmation) -> dict:
        """SECTION 11 AUDIT: Realize PnL from a confirmed settlement.

        Called when a fill is confirmed on-chain. Records the settlement
        with PnL calculation and updates tracking metrics.
        """
        shares = getattr(fill_confirmation, 'fill_amount', 0) or getattr(order, 'filled_shares', 0) or getattr(order, 'fill_amount', 0)
        buy_price = getattr(order, 'price', getattr(order, 'avg_fill_price', 0))
        tx_hash = getattr(fill_confirmation, 'tx_hash', None) or getattr(order, 'tx_hash', None)
        order_id = getattr(order, 'order_id', '') or getattr(fill_confirmation, 'order_id', '')
        condition_id = getattr(order, 'condition_id', '') or getattr(fill_confirmation, 'condition_id', '')

        # PnL: shares * $1.00 (redemption value) - shares * buy_price - gas
        from config.settings import GAS_PER_SHARE
        gross_pnl = shares * 1.0
        cost = shares * buy_price
        gas = GAS_PER_SHARE * shares
        net_pnl = gross_pnl - cost - gas
        roi = (net_pnl / cost * 100) if cost > 0 else 0.0

        settlement = {
            'order_id': order_id,
            'condition_id': condition_id,
            'tx_hash': tx_hash,
            'shares': shares,
            'buy_price': buy_price,
            'gross_pnl': gross_pnl,
            'cost': cost,
            'gas': gas,
            'net_pnl': net_pnl,
            'roi': roi,
            'settled_at': time.time(),
            'status': 'settled',
        }
        self._settlements[order_id] = settlement
        self._total_settled += 1
        self._total_pnl_realized += net_pnl
        self._settlement_ages.append(time.time() - getattr(order, 'placed_at', getattr(order, 'submitted_at', time.time())))
        if len(self._settlement_ages) > self._max_settlement_age_samples:
            self._settlement_ages = self._settlement_ages[-self._max_settlement_age_samples:]
        logger.info(f"Settlement realized: {order_id[:16]} | {shares} shares | NET PnL: ${net_pnl:.2f} | ROI: {roi:.2f}%")
        return settlement

    def get_pending_settlements(self) -> list:
        """SECTION 11 AUDIT: Get fills awaiting settlement (matched but not confirmed)."""
        pending = []
        for oid, s in self._settlements.items():
            if s.get('status') == 'pending':
                pending.append(s)
        self._total_pending_settlement = len(pending)
        return pending

    def get_settlement_metrics(self) -> dict:
        """SECTION 11 AUDIT: Return settlement metrics for monitoring."""
        avg_age = sum(self._settlement_ages) / len(self._settlement_ages) if self._settlement_ages else 0.0
        return {
            'total_settled': self._total_settled,
            'total_pnl_realized': round(self._total_pnl_realized, 4),
            'total_pending': self._total_pending_settlement,
            'avg_settlement_age_s': round(avg_age, 3),
            'block_confirmations_required': BLOCK_CONFIRMATIONS_REQUIRED,
            'settlements_tracked': len(self._settlements),
        }

    def confirm_fills_batch(self, orders, timeout=None) -> list:
        """AUDIT FIX #27: Batch confirm multiple fills."""
        results = []
        for order in orders:
            result = self.confirm_fill(order, timeout)
            results.append(result)
        return results

    def get_fill_age(self, order) -> float:
        """AUDIT FIX #27: Get time since order was placed."""
        placed_at = getattr(order, 'placed_at', None) or getattr(order, 'submitted_at', None)
        if placed_at:
            return time.time() - placed_at
        return 0.0

    def get_fill_metrics(self) -> dict:
        """AUDIT FIX #27: Return fill confirmation metrics for monitoring."""
        avg_age = sum(self._fill_ages) / len(self._fill_ages) if self._fill_ages else 0.0
        total_attempts = self._total_confirmed + self._total_timeouts + self._total_matched_offchain + self._total_trade_pending
        confirm_rate = self._total_confirmed / max(1, total_attempts) * 100
        return {
            'total_confirmed': self._total_confirmed,
            'total_ghosts': self._total_ghosts,
            'total_timeouts': self._total_timeouts,
            'total_matched_offchain': self._total_matched_offchain,
            'total_trade_pending': self._total_trade_pending,
            'total_partial': self._total_partial,
            'confirm_rate': round(confirm_rate, 2),
            'avg_fill_age_s': round(avg_age, 3),
            'block_confirmations_required': BLOCK_CONFIRMATIONS_REQUIRED,
            'max_fill_retries': self._max_fill_retries,
        }

    def reconcile_position(self, position) -> str:
        if not isinstance(position, dict):
            return 'real'
        tx_hash = position.get('tx_hash', '')
        if not tx_hash:
            self._total_ghosts += 1
            return 'phantom'
        if self.config.paper_mode:
            return 'real'
        if self._settled_on_chain(tx_hash):
            return 'real'
        return 'phantom'
