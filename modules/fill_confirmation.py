"""Sweeper Bot V2 - Fill Confirmation

P0 #2: Fixed V2 constructor: chain=137 -> chain_id=137
P0 #3: Added signature_type and funder parameters
P0 #6: Fixed txHash -> transactionsHashes (list) for V2 response format
"""
import time
import logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum

logger = logging.getLogger("sweeper.fillconfirm")

class FillStatus(Enum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    GHOST = "ghost"
    TIMEOUT = "timeout"
    PAPER = "paper"

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
        if self.config.paper_mode:
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
        while time.time() - start < timeout:
            client = self._get_client()
            if client:
                try:
                    status = client.get_order(getattr(order, 'order_id', ''))
                    if isinstance(status, dict):
                        matched = float(status.get('size_matched', 0))
                        if matched > 0:
                            tx_hashes = status.get('transactionsHashes', [])
                            tx_hash = tx_hashes[0] if tx_hashes else status.get('txHash', '')
                            if self._settled_on_chain(tx_hash):
                                return FillConfirmation(order.order_id, order.condition_id, FillStatus.CONFIRMED, matched, tx_hash, time.time())
                except Exception as e:
                    logger.debug(f"Order check error: {e}")
            time.sleep(0.5)
        return FillConfirmation(getattr(order, 'order_id', ''), getattr(order, 'condition_id', ''), FillStatus.TIMEOUT, 0, None, time.time())

    def _settled_on_chain(self, tx_hash: str) -> bool:
        if not tx_hash:
            return False
        w3 = self._get_web3()
        if not w3:
            return True
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            return receipt is not None and receipt.get('status') == 1
        except Exception:
            return False

    def reconcile_position(self, position) -> str:
        if not isinstance(position, dict):
            return 'real'
        tx_hash = position.get('tx_hash', '')
        if not tx_hash:
            return 'phantom'
        if self.config.paper_mode:
            return 'real'
        if self._settled_on_chain(tx_hash):
            return 'real'
        return 'phantom'
