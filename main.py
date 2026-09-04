"""Sweeper Bot V2 - Main Orchestrator with GTC Post-Only

FIX #2: Standardized fill probability logic (35% fill, 25% partial, 5% ghost, 35% expired)
FIX #3: Gas cost standardized to GAS_PER_SHARE (0.001/share)
P0 #3: Added SIGNATURE_TYPE and FUNDER_ADDRESS env var reading for V2 SDK wallet support
P0 #8/#10: _complete_trade now creates positions in open_positions
P0 #11: startup_reconcile uses StartupRecovery instead of killing on resting orders

AUDIT FIX #1: Block --live mode while P0 audit items remain open
AUDIT FIX #2: Add process lock to prevent duplicate bot instances
AUDIT FIX #9: Cancel all remote orders when kill switch activates
"""
import sys, os, time, json, signal, logging, threading
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SweeperConfig, fee_per_share, net_edge_per_share, GAS_PER_SHARE, get_fee_rate
from modules.safety_rails import SafetyRails
from modules.market_discovery import MarketDiscovery
from modules.resolution_detection import ResolutionDetector
from modules.order_executor import OrderBuilder, RestingOrder, OrderStatus, plan_entry
from modules.rate_limiter import RateLimitManager
from modules.fill_confirmation import FillConfirmer
from modules.reconciliation import ReconciliationEngine
from modules.gas_manager import GasManager
from modules.capital_recycler import CapitalRecycler
from modules.startup_recovery import StartupRecovery

logger = logging.getLogger("sweeper.main")

# AUDIT FIX #1: Block live mode until all P0 audit items are closed
P0_BLOCKED = True  # Set to False ONLY after all P0 production-readiness items are resolved

# AUDIT FIX #2: Process lock to prevent duplicate instances
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

def acquire_process_lock():
    """Prevent duplicate bot instances via file lock."""
    if not _HAS_FCNTL:
        return None  # Non-Unix systems - skip lock
    lock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.sweeper.lock')
    try:
        lock = open(lock_file, 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write(str(os.getpid()))
        lock.flush()
        logger.info(f"Process lock acquired (PID {os.getpid()})")
        return lock
    except (IOError, OSError):
        logger.critical("FATAL: Another sweeper-bot-v2 instance is already running.")
        print("FATAL: Another sweeper-bot-v2 instance is already running.")
        sys.exit(1)

class SweeperBot:
    def __init__(self, config=None):
        self.config = config or SweeperConfig(paper_mode=True)
        self.safety = SafetyRails(self.config)
        self.discovery = MarketDiscovery(self.config)
        self.detector = ResolutionDetector(self.config)
        self.order_builder = OrderBuilder(self.config, self.safety)
        self.rate_limiter = RateLimitManager(self.config)
        self.fill_confirmer = FillConfirmer(self.config)
        self.reconciler = ReconciliationEngine(self.config, self.safety, self.fill_confirmer, self.order_builder)
        self.gas = GasManager(self.config, self.safety)
        self.recycler = CapitalRecycler(self.config, self.order_builder, self.safety)
        self._running = False
        self._cycle_count = 0
        self._shutdown_requested = False
        self._lock = None

    def startup_reconcile(self):
        logger.info("=" * 60)
        logger.info("STARTUP RECONCILIATION")
        logger.info("=" * 60)
        ok, checks = self.safety.preflight_check()
        for c in checks:
            logger.info(f"  {c}")
        if not ok:
            logger.critical("Preflight FAILED")
            return False
        loaded = self.safety.load_state()
        if loaded:
            logger.info(f"State restored: {len(self.safety.state.worked_markets)} worked markets")
        recovery = StartupRecovery(self.config, self.order_builder, self.safety)
        recovery_result = recovery.recover()
        if recovery_result['orders_recovered'] > 0 or recovery_result['positions_recovered'] > 0:
            logger.info(f"Remote recovery: {recovery_result['orders_recovered']} orders, {recovery_result['positions_recovered']} positions")
        resting = self.order_builder.list_open_orders()
        if resting:
            logger.info(f"Total resting orders: {len(resting)} (including recovered)")
        gas_status = self.gas.check_balance()
        logger.info(f"Gas: {gas_status.balance_pol} POL | Low: {gas_status.is_low} | Critical: {gas_status.is_critical}")
        if gas_status.is_critical:
            logger.critical("Gas CRITICAL")
            return False
        logger.info("Startup reconciliation COMPLETE")
        return True

    def run_cycle(self):
        self._cycle_count += 1
        logger.info(f"--- CYCLE {self._cycle_count} ---")
        killed, reason = self.safety.check_kill_switch()
        if killed:
            logger.critical(f"Kill switch: {reason}")
            # AUDIT FIX #9: Cancel all remote orders when kill switch activates
            cancelled = self.order_builder.cancel_all()
            logger.critical(f"Kill switch: cancelled {cancelled} remote orders")
            self.safety.dump_state()
            return False
        try:
            candidates = self.discovery.discover_candidates(max_markets=100)
            logger.info(f"Discovered {len(candidates)} markets")
        except Exception as e:
            logger.error(f"Discovery failed: {e}")
            return True
        sweepable = []
        for m in candidates:
            try:
                det = self.detector.detect(m)
                if det and self.detector.is_sweepable(det):
                    sweepable.append(det)
            except Exception:
                pass
        logger.info(f"{len(sweepable)} sweepable markets")
        placed = 0
        for det in sweepable:
            if placed >= 10:
                break
            if self.safety.is_worked(det.condition_id):
                continue
            if not self.rate_limiter.can_request("order"):
                logger.warning("Order rate limit exhausted")
                break
            best_ask = None
            try:
                book = self.discovery.get_market_book(det.winning_token_id)
                asks = book.get("asks", [])
                if asks:
                    best_ask = min(float(a.get("price", 0)) for a in asks)
            except Exception:
                pass
            tick_size = getattr(det, 'tick_size', 0.001 if det.winning_price >= 0.999 else 0.01)
            success, order = self.order_builder.build_and_place(detection_result=det, size=100.0, best_ask=best_ask, tick_size=tick_size, neg_risk=getattr(det, 'neg_risk', False))
            if success and order:
                self.rate_limiter.record_request("order")
                self.safety.mark_worked(det.condition_id)
                placed += 1
                if isinstance(order, RestingOrder):
                    logger.info(f"GTC post-only: {order.order_id} @ {order.price} for {det.question[:40]}")
                    if self.config.paper_mode:
                        self._paper_fill(order, det)
                else:
                    logger.info(f"FAK taker: {order.order_id} for {det.question[:40]}")
            else:
                logger.debug(f"Order rejected for {det.question[:40]}")
        self._reconcile()
        self.safety.dump_state()
        logger.info(f"Cycle {self._cycle_count}: {placed} orders placed")
        return True

    def _paper_fill(self, order, det):
        import random
        rng = random.Random()
        roll = rng.random()
        if roll < self.config.fill_probability:
            order.filled_shares = order.shares
            order.avg_fill_price = order.price
            order.tx_hash = f"paper_tx_{int(time.time())}"
            order.status = OrderStatus.FILLED
            logger.info(f"[PAPER] Maker fill: {order.shares} @ {order.price}")
            self._complete_trade(det, order, True, order.filled_shares, order.price)
        elif roll < self.config.fill_probability + self.config.partial_fill_probability:
            partial = max(1, int(order.shares * self.config.partial_fill_ratio))
            order.filled_shares = float(partial)
            order.avg_fill_price = order.price
            order.tx_hash = f"paper_tx_{int(time.time())}"
            order.status = OrderStatus.PARTIAL
            logger.info(f"[PAPER] Partial fill: {partial}/{order.shares}")
            self._complete_trade(det, order, True, order.filled_shares, order.price)
        elif roll < self.config.fill_probability + self.config.partial_fill_probability + self.config.ghost_probability:
            order.tx_hash = None
            order.status = OrderStatus.LIVE
            logger.warning("[PAPER] Ghost fill detected")
        else:
            order.status = OrderStatus.EXPIRED
            logger.info("[PAPER] Order expired")
            self.safety.unmark_worked(det.condition_id)

    def _complete_trade(self, det, order, is_maker, filled_shares, fill_price):
        if not order.tx_hash:
            logger.warning("Ghost fill - no on-chain settlement")
            return
        condition_id = getattr(det, 'condition_id', '')
        if condition_id:
            self.safety.state.open_positions[condition_id] = {
                'condition_id': condition_id,
                'token_id': getattr(order, 'token_id', getattr(det, 'winning_token_id', '')),
                'shares': filled_shares,
                'fill_price': fill_price,
                'tx_hash': order.tx_hash,
                'status': 'open',
                'is_maker': is_maker,
                'is_paper': getattr(order, 'is_paper', False),
                'timestamp': time.time(),
            }
        try:
            self.recycler.recycle(det, filled_shares)
        except Exception as e:
            logger.error(f"Recycle error: {e}")
        if condition_id and condition_id in self.safety.state.open_positions:
            self.safety.state.open_positions[condition_id]['status'] = 'closed'
            self.safety.state.open_positions[condition_id]['closed_at'] = time.time()
        gross = (1.0 - fill_price) * filled_shares
        fee = fee_per_share(fill_price, is_maker=is_maker) * filled_shares
        gas_cost = GAS_PER_SHARE * filled_shares
        net = gross - fee - (self.config.loser_max_price * filled_shares) - gas_cost
        self.safety.update_scoreboard(buys=[{}], redeems=[{}], merges=[{"amount": filled_shares}], net_pnl=net)
        self.safety.state.daily_pnl += net
        logger.info(f"Trade complete: {'MAKER' if is_maker else 'TAKER'} | PnL: ${net:.4f} | Daily: ${self.safety.state.daily_pnl:.4f}")

    def _reconcile(self):
        if self.reconciler.should_run():
            try:
                result = self.reconciler.reconcile()
                logger.info(f"Position reconcile: {result.total_positions} pos, {result.phantom_positions} phantoms")
            except Exception as e:
                logger.error(f"Reconcile error: {e}")
        if self.reconciler.should_run_orders():
            try:
                result = self.reconciler.reconcile_orders()
                if result.filled:
                    logger.info(f"Order reconcile: {len(result.filled)} filled, {result.still_resting} resting")
            except Exception as e:
                logger.error(f"Order reconcile error: {e}")

    def shutdown(self):
        logger.info("=" * 60)
        logger.info("SHUTDOWN INITIATED")
        logger.info("=" * 60)
        self._running = False
        if self.config.cancel_orders_on_shutdown:
            count = self.order_builder.shutdown()
            logger.info(f"Cancelled {count} resting orders")
        self.safety.dump_state()
        logger.info("Shutdown complete")

    def _signal_handler(self, signum, frame):
        logger.info(f"Signal {signum} received")
        self._shutdown_requested = True

    def run(self):
        # AUDIT FIX #2: Acquire process lock to prevent duplicate instances
        self._lock = acquire_process_lock()
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        if not self.startup_reconcile():
            return
        self._running = True
        logger.info("=" * 60)
        logger.info("SWEEPER BOT V2 - RUNNING")
        logger.info(f"  Mode: {'PAPER' if self.config.paper_mode else 'LIVE'}")
        logger.info(f"  Order Method: {'GTC POST-ONLY MAKER' if self.config.prefer_maker else 'FAK TAKER'}")
        logger.info(f"  Taker Fallback: {self.config.allow_taker_fallback}")
        logger.info(f"  Resting Timeout: {self.config.resting_order_timeout}s | Reconcile: {self.config.order_reconcile_interval}s")
        logger.info(f"  Cancel on Shutdown: {self.config.cancel_orders_on_shutdown}")
        logger.info("=" * 60)
        while self._running and not self._shutdown_requested:
            try:
                ok = self.run_cycle()
                if not ok:
                    break
                time.sleep(5)
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                time.sleep(10)
        self.shutdown()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sweeper Bot V2")
    parser.add_argument("--paper", action="store_true", default=True, help="Paper mode")
    parser.add_argument("--live", action="store_true", help="Live trading mode")
    parser.add_argument("--cycles", type=int, default=0, help="Max cycles (0 = infinite)")
    args = parser.parse_args()
    # AUDIT FIX #1: Block live mode while P0 audit items are open
    if args.live and P0_BLOCKED:
        print("=" * 60)
        print("FATAL: Live mode is BLOCKED.")
        print("P0 production-readiness audit items remain open.")
        print("Set P0_BLOCKED = False in main.py ONLY after all P0")
        print("findings are resolved and the final release gate passes.")
        print("=" * 60)
        sys.exit(1)
    config = SweeperConfig(paper_mode=not args.live)
    config.private_key = os.environ.get("PRIVATE_KEY", "")
    config.clob_api_key = os.environ.get("CLOB_API_KEY", "")
    config.clob_api_secret = os.environ.get("CLOB_API_SECRET", "")
    config.clob_api_passphrase = os.environ.get("CLOB_API_PASSPHRASE", "")
    config.wallet_address = os.environ.get("WALLET_ADDRESS", "")
    config.signature_type = int(os.environ.get("SIGNATURE_TYPE", "0"))
    config.funder = os.environ.get("FUNDER_ADDRESS", "")
    bot = SweeperBot(config)
    if args.cycles > 0:
        if not bot.startup_reconcile():
            sys.exit(1)
        for i in range(args.cycles):
            if not bot.run_cycle():
                break
            time.sleep(2)
        bot.shutdown()
    else:
        bot.run()
