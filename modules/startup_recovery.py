"""Sweeper Bot V2 - Startup Recovery

P0 #11: Query remote open orders and nonterminal trades on startup
instead of checking empty local memory. Recovers resting orders and
positions from the CLOB V2 API after a restart.
"""
import time
import logging

logger = logging.getLogger("sweeper.recovery")

class StartupRecovery:
    def __init__(self, config, order_builder, safety_rails):
        self.config = config
        self.builder = order_builder
        self.safety = safety_rails

    def recover(self):
        """Recover resting orders and positions from remote state."""
        if self.config.paper_mode:
            logger.info("Startup recovery: Paper mode — remote recovery skipped")
            return {'orders_recovered': 0, 'trades_recovered': 0, 'positions_recovered': 0}

        client = self.builder._get_client()
        if not client:
            logger.warning("Startup recovery: CLOB V2 client not available — skipping")
            return {'orders_recovered': 0, 'trades_recovered': 0, 'positions_recovered': 0}

        orders_recovered = 0
        trades_recovered = 0
        positions_recovered = 0

        # Recover open orders from remote
        try:
            open_orders = client.get_open_orders()
            if open_orders:
                for order_data in open_orders:
                    if isinstance(order_data, dict):
                        order_id = order_data.get('id', order_data.get('orderID', ''))
                        if order_id and order_id not in self.builder._resting:
                            from modules.order_executor import RestingOrder, OrderStatus
                            order = RestingOrder(
                                order_id=order_id,
                                condition_id=order_data.get('condition_id', ''),
                                token_id=order_data.get('asset_id', order_data.get('token_id', '')),
                                market_question=order_data.get('question', 'Recovered order'),
                                side=order_data.get('side', 'BUY'),
                                price=float(order_data.get('price', 0)),
                                shares=float(order_data.get('original_size', order_data.get('size', 0))),
                                tick_size=order_data.get('tick_size', '0.01'),
                                neg_risk=order_data.get('neg_risk', False),
                                status=OrderStatus.LIVE,
                                is_paper=False,
                            )
                            self.builder._resting[order_id] = order
                            self.builder._reserved[order_id] = order.shares * order.price
                            orders_recovered += 1
                logger.info(f"Startup recovery: Recovered {orders_recovered} open orders from remote")
        except Exception as e:
            logger.error(f"Startup recovery: Failed to get open orders: {e}")

        # Recover nonterminal trades and create positions
        try:
            trades = client.get_trades()
            if trades:
                for trade_data in trades:
                    if isinstance(trade_data, dict):
                        condition_id = trade_data.get('condition_id', '')
                        if condition_id and condition_id not in self.safety.state.open_positions:
                            tx_hashes = trade_data.get('transactionsHashes', [])
                            tx_hash = tx_hashes[0] if tx_hashes else trade_data.get('transaction_hash', '')
                            self.safety.state.open_positions[condition_id] = {
                                'condition_id': condition_id,
                                'token_id': trade_data.get('asset_id', trade_data.get('token_id', '')),
                                'shares': float(trade_data.get('size', 0)),
                                'fill_price': float(trade_data.get('price', 0)),
                                'tx_hash': tx_hash,
                                'status': 'open',
                                'is_maker': trade_data.get('maker_address', '') != '',
                                'is_paper': False,
                                'timestamp': time.time(),
                            }
                            positions_recovered += 1
                trades_recovered = len(trades)
                logger.info(f"Startup recovery: Recovered {trades_recovered} trades, {positions_recovered} positions from remote")
        except Exception as e:
            logger.error(f"Startup recovery: Failed to get trades: {e}")

        return {'orders_recovered': orders_recovered, 'trades_recovered': trades_recovered, 'positions_recovered': positions_recovered}
