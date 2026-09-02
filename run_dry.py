"""Sweeper Bot V2 - Advanced Dry Run with GTC Post-Only Maker Orders"""
import sys, os, time, json, logging, random
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SweeperConfig, fee_per_share, net_edge_per_share
from modules.safety_rails import SafetyRails
from modules.market_discovery import MarketDiscovery
from modules.resolution_detection import ResolutionDetector
from modules.order_executor import OrderBuilder, RestingOrder, OrderStatus, plan_entry
from modules.rate_limiter import RateLimitManager
from modules.fill_confirmation import FillConfirmer
from modules.reconciliation import ReconciliationEngine
from modules.gas_manager import GasManager
from modules.capital_recycler import CapitalRecycler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
TS = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
MAIN_LOG = os.path.join(LOG_DIR, f"dry_run_{TS}.log")
TRADE_LOG = os.path.join(LOG_DIR, f"trades_{TS}.log")
MARKET_LOG = os.path.join(LOG_DIR, f"markets_{TS}.log")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S", handlers=[logging.FileHandler(MAIN_LOG), logging.StreamHandler()])
logger = logging.getLogger("sweeper.dryrun")

class AdvancedDryRunner:
    def __init__(self):
        self.config = SweeperConfig(paper_mode=True)
        self.safety = SafetyRails(self.config)
        self.discovery = MarketDiscovery(self.config)
        self.detector = ResolutionDetector(self.config)
        self.order_builder = OrderBuilder(self.config)
        self.rate_limiter = RateLimitManager(self.config)
        self.fill_confirmer = FillConfirmer(self.config)
        self.reconciler = ReconciliationEngine(self.config, self.safety, self.fill_confirmer, self.order_builder)
        self.gas = GasManager(self.config, self.safety)
        self.recycler = CapitalRecycler(self.config, self.order_builder, self.safety)
        self.cumulative_pnl = 0.0; self.daily_pnl = 0.0
        self.total_trades = 0; self.winning_trades = 0
        self.maker_fills = 0; self.taker_fills = 0
        self.resting_orders = 0; self.expired_orders = 0; self.partial_fills = 0; self.ghost_fills = 0
        self.total_recycled = 0.0; self.recycle_count = 0
        self.errors = []; self.cycle_num = 0

    def log(self, msg): logger.info(msg)
    def log_trade(self, msg):
        with open(TRADE_LOG, "a") as f: f.write(msg + "\n")
    def log_market(self, msg):
        with open(MARKET_LOG, "a") as f: f.write(msg + "\n")

    def run_cycle(self, max_sweeps=10):
        self.cycle_num += 1; cycle_start = time.time()
        self.log(""); self.log("=" * 80); self.log(f"CYCLE {self.cycle_num}"); self.log("=" * 80)
        self.log("[DISCOVERY] Fetching live markets from Gamma API...")
        try:
            candidates = self.discovery.discover_candidates(max_markets=100)
            self.log(f"[DISCOVERY] Found {len(candidates)} candidate markets")
        except Exception as e:
            self.log(f"ERROR: Discovery failed: {e}"); self.errors.append(f"Cycle {self.cycle_num}: {e}"); return False
        self.log(""); self.log("[MARKET DATA] Top 5 candidates:")
        for i, m in enumerate(sorted(candidates, key=lambda m: m.sweep_score, reverse=True)[:5], 1):
            self.log(f"  {i}. {m.question[:60]}")
            self.log(f"     Vol: ${m.volume_24hr:,.0f} | YES={m.yes_price} | NO={m.no_price} | Score: {m.sweep_score:.4f}")
            self.log_market(f"MARKET|{m.condition_id}|{m.question}|{m.volume_24hr}|{m.end_date}|{m.neg_risk}|{m.sweep_score:.4f}")
        self.log(""); self.log("[DETECTION] Running resolution detection...")
        sweepable = []
        for m in candidates:
            try:
                det = self.detector.detect(m)
                if det and self.detector.is_sweepable(det): sweepable.append(det)
            except Exception: pass
        self.log(f"[DETECTION] {len(sweepable)} sweepable markets")
        self.log(""); self.log("[PRICE ACTION] Sweepable markets:")
        for i, det in enumerate(sweepable[:10], 1):
            self.log(f"  {i}. {det.question[:60]}")
            self.log(f"     Win: {det.winning_side} @ {det.winning_price} | Lose @ {det.losing_price} | Spread: {det.winning_price - det.losing_price:.4f}")
            self.log(f"     Certainty: {det.certainty} | Confidence: {det.confidence_score:.2f}% | Reason: {det.detection_reason}")
            self.log_market(f"PRICE_ACTION|{det.condition_id}|{det.winning_side}|{det.winning_price}|{det.losing_price}|{det.winning_price - det.losing_price:.4f}|{det.certainty}|{det.confidence_score:.2f}%")
        self.log(""); sweeps = 0
        for det in sweepable:
            if sweeps >= max_sweeps: break
            if self.safety.is_worked(det.condition_id): continue
            killed, reason = self.safety.check_kill_switch()
            if killed: self.log(f"[KILL SWITCH] {reason}"); return False
            if not self.rate_limiter.can_request("order"): self.log("[RATE LIMIT] Order budget exhausted"); break
            best_ask = None; best_bid_price = None; best_ask_price = None
            book_depth_asks = 0; book_depth_bids = 0
            try:
                book = self.discovery.get_market_book(det.winning_token_id)
                asks = book.get("asks", []); bids = book.get("bids", [])
                book_depth_asks = len(asks); book_depth_bids = len(bids)
                if asks: best_ask = max(float(a.get("price", 0)) for a in asks); best_ask_price = best_ask
                if bids: best_bid_price = max(float(b.get("price", 0)) for b in bids)
            except Exception: pass
            tick_size = 0.001 if det.winning_price >= 0.999 else 0.01
            entry_plan = None
            if self.config.prefer_maker and best_ask is not None:
                entry_plan = plan_entry(best_ask, tick_size, 0.985, self.config.buy_price, self.config.prefer_maker, self.config.allow_taker_fallback)
            is_maker = (entry_plan is not None and entry_plan[1]) or (self.config.prefer_maker and best_ask is None)
            order_price = entry_plan[0] if entry_plan else self.config.buy_price
            order_detail = entry_plan[2] if entry_plan else "maker bid at buy_price"
            self.log(""); self.log("-" * 60); self.log(f"TRADE #{self.total_trades + 1}"); self.log("-" * 60)
            self.log(f"  Market: {det.question[:70]}")
            self.log(f"  Condition: {det.condition_id[:50]}...")
            self.log("  PRICE ACTION:")
            self.log(f"    Winning: {det.winning_side} @ {det.winning_price} | Losing @ {det.losing_price} | Spread: {det.winning_price - det.losing_price:.4f}")
            self.log(f"    Certainty: {det.certainty} | Confidence: {det.confidence_score:.2f}% | Detection: {det.detection_reason}")
            if best_ask_price: self.log(f"    Best Ask: {best_ask_price} | Best Bid: {best_bid_price}")
            self.log(f"    Order Book: {book_depth_asks} asks, {book_depth_bids} bids")
            self.log(""); self.log("  ORDER EXECUTION:")
            if is_maker:
                self.log("    Method: GTC POST-ONLY MAKER (zero fees)")
                self.log(f"    Entry: {order_detail}")
                self.log(f"    Building GTC post-only bid: BUY 100 @ ${order_price}")
            else:
                self.log("    Method: FAK TAKER (pays fees)")
                self.log(f"    Building FAK order: BUY 100 @ ${order_price}")
            success, order = self.order_builder.build_and_place(detection_result=det, size=100.0, best_ask=best_ask, tick_size=tick_size, neg_risk=getattr(det, 'neg_risk', False))
            if not success or order is None:
                self.log(f"    ORDER REJECTED: {order.error if order else 'No entry price'}")
                self.errors.append(f"Cycle {self.cycle_num} Trade {self.total_trades+1}: Rejected"); continue
            if isinstance(order, RestingOrder):
                self.log(f"    Order ID: {order.order_id} | Status: {order.status.value} | Paper: True")
                if order.status == OrderStatus.LIVE:
                    self.resting_orders += 1
                    self.log(f"    QUEUED: Resting on book | Fill Prob: {self.config.fill_probability*100:.0f}% | Timeout: {self.config.resting_order_timeout:.0f}s")
                    self.log(""); self.log("  ORDER RECONCILIATION (fast-forward):")
                    fill_result = self._simulate_resting_fill(order, det)
                    if fill_result == "filled":
                        self.maker_fills += 1; self.log(f"    Result: MAKER FILL (zero fees) - {order.filled_shares} @ ${order.price}")
                        self.log(f"    TX: {order.tx_hash}"); self._complete_trade(det, order, True, order.filled_shares, order.price)
                    elif fill_result == "partial":
                        self.partial_fills += 1; self.log(f"    Result: PARTIAL FILL - {order.filled_shares}/100"); self._complete_trade(det, order, True, order.filled_shares, order.price)
                    elif fill_result == "ghost":
                        self.ghost_fills += 1; self.log("    Result: GHOST FILL (off-chain match, on-chain revert)")
                    elif fill_result == "expired":
                        self.expired_orders += 1; self.log("    Result: EXPIRED | Market released"); self.safety.unmark_worked(det.condition_id)
                elif order.status in (OrderStatus.MATCHED, OrderStatus.FILLED):
                    self.maker_fills += 1; filled = order.filled_shares if order.filled_shares > 0 else 100.0
                    self.log(f"    Result: INSTANT MAKER FILL - {filled} @ ${order.price}"); self._complete_trade(det, order, True, filled, order.price)
                elif order.status == OrderStatus.PARTIAL:
                    self.partial_fills += 1; self.log(f"    Result: PARTIAL - {order.filled_shares}/100"); self._complete_trade(det, order, True, order.filled_shares, order.price)
            else:
                self.taker_fills += 1; self.log(f"    FILL: {order.fill_amount} | TX: {order.tx_hash}")
                self._complete_trade(det, order, False, order.fill_amount, order.price)
            self.rate_limiter.record_request("order"); sweeps += 1; self.safety.mark_worked(det.condition_id)
        cycle_time = time.time() - cycle_start
        self.log(""); self.log("=" * 80); self.log(f"CYCLE {self.cycle_num} SUMMARY"); self.log("=" * 80)
        self.log(f"  Sweeps: {sweeps} | Time: {cycle_time:.2f}s | PnL: ${self.cumulative_pnl:.4f} | Daily: ${self.daily_pnl:.4f}")
        self.log(f"  Trades: {self.total_trades} | Win: {self.winning_trades}" + (f" = {self.winning_trades/self.total_trades*100:.1f}%" if self.total_trades > 0 else ""))
        self.log(f"  Maker: {self.maker_fills} (zero fees) | Taker: {self.taker_fills} | Resting: {self.resting_orders} | Expired: {self.expired_orders} | Partial: {self.partial_fills} | Ghost: {self.ghost_fills}")
        for b in ["order", "book", "gamma", "api_key", "relayer"]: self.log(f"    {b}: {self.rate_limiter.remaining(b)} remaining")
        gs = self.gas.check_balance(); self.log(f"  Gas: {gs.balance_pol} POL | Low: {gs.is_low}")
        try:
            r = self.reconciler.reconcile(); self.log(f"  [RECONCILE] {r.total_positions} pos, {r.phantom_positions} phantoms")
        except Exception as e: self.log(f"  [RECONCILE] Error: {e}")
        resting = self.order_builder.list_open_orders(); exp = self.safety.get_exposure(resting)
        self.log(f"  [EXPOSURE] Pos: ${exp['position_exposure']} | Resting: ${exp['resting_exposure']} | Total: ${exp['total_exposure']}")
        self.safety.dump_state(); return True

    def _simulate_resting_fill(self, order, det):
        rng = random.Random()
        if rng.random() < self.config.fill_probability:
            order.filled_shares = order.shares; order.avg_fill_price = order.price
            order.tx_hash = f"paper_tx_{int(time.time())}"; order.status = OrderStatus.FILLED; return "filled"
        if rng.random() < self.config.ghost_probability:
            order.filled_shares = order.shares; order.avg_fill_price = order.price
            order.tx_hash = None; order.status = OrderStatus.LIVE; return "ghost"
        if rng.random() < self.config.partial_fill_probability:
            p = max(1, int(order.shares * self.config.partial_fill_ratio))
            order.filled_shares = float(p); order.avg_fill_price = order.price
            order.tx_hash = f"paper_tx_{int(time.time())}"; order.status = OrderStatus.PARTIAL; return "partial"
        if rng.random() < 0.3:
            order.filled_shares = order.shares; order.avg_fill_price = order.price
            order.tx_hash = f"paper_tx_{int(time.time())}"; order.status = OrderStatus.FILLED; return "filled"
        order.status = OrderStatus.EXPIRED; return "expired"

    def _complete_trade(self, det, order, is_maker, filled_shares, fill_price):
        self.total_trades += 1; self.log(""); self.log("  FILL CONFIRMATION:")
        if order.tx_hash:
            self.log(f"    Confirmed: {filled_shares} shares | TX: {order.tx_hash} | On-chain: True")
        else: self.log("    GHOST FILL: No settlement"); self.ghost_fills += 1; return
        self.winning_trades += 1; self.log(""); self.log("  CAPITAL RECYCLE:")
        self.log(f"    Buying loser @ ${self.config.loser_max_price}/share | Merging: {filled_shares} YES + {filled_shares} NO -> {filled_shares} pUSD")
        try: self.recycler.recycle(det, filled_shares)
        except Exception as e: self.log(f"    Recycle error: {e}")
        recycled_usd = filled_shares * 1.0; loser_cost = filled_shares * self.config.loser_max_price
        self.log(f"    Recycled: ${recycled_usd:.2f} USDC | Loser cost: ${loser_cost:.4f}")
        self.total_recycled += recycled_usd; self.recycle_count += 1
        self.log(""); self.log("  PNL BREAKDOWN:")
        gross = (1.0 - fill_price) * filled_shares; fee = fee_per_share(fill_price, is_maker=is_maker) * filled_shares
        gas_cost = 0.001 * filled_shares; net_pnl = gross - fee - loser_cost - gas_cost
        self.log(f"    Type: {'GTC POST-ONLY MAKER' if is_maker else 'FAK TAKER'}")
        self.log(f"    Gross Edge: ${gross:.4f} | Fee: ${fee:.4f} ({'ZERO' if is_maker else 'taker'}) | Loser: ${loser_cost:.4f} | Gas: ${gas_cost:.4f}")
        self.log(f"    Net PnL: +${net_pnl:.4f}")
        self.cumulative_pnl += net_pnl; self.daily_pnl += net_pnl
        self.log(f"    Cumulative: ${self.cumulative_pnl:.4f} | Daily: ${self.daily_pnl:.4f} / Max: ${self.config.max_daily_loss}")
        self.log(f"    Win Rate: {self.winning_trades}/{self.total_trades}" + (f" = {self.winning_trades/self.total_trades*100:.1f}%" if self.total_trades > 0 else ""))
        self.safety.update_scoreboard(buys=[{}], redeems=[{}], merges=[{"amount": recycled_usd}])
        self.log_trade(f"TRADE|{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}|{det.condition_id[:16]}|{det.winning_side}|{det.winning_price}|{det.losing_price}|{filled_shares}|{fill_price}|{gross:.4f}|{loser_cost:.4f}|{fee:.4f}|{net_pnl:.4f}|{self.cumulative_pnl:.4f}|{self.daily_pnl:.4f}|{self.winning_trades}/{self.total_trades}|{'MAKER' if is_maker else 'TAKER'}")
        self.log(""); self.log(f"  TRADE #{self.total_trades} COMPLETE - {'MAKER (ZERO FEES)' if is_maker else 'TAKER'}"); self.log("-" * 60)

    def run(self, cycles=3, max_sweeps=10):
        self.log("=" * 80); self.log("SWEEPER BOT V2 - ADVANCED DRY RUN WITH GTC POST-ONLY MAKER ORDERS"); self.log("=" * 80)
        self.log(f"Mode: PAPER | Buy: ${self.config.buy_price} | Method: GTC POST-ONLY MAKER (zero fees)")
        self.log(f"Taker Fallback: DISABLED | Resting Timeout: {self.config.resting_order_timeout:.0f}s | Reconcile: {self.config.order_reconcile_interval:.0f}s")
        self.log(f"Fill Prob: {self.config.fill_probability*100:.0f}% | Ghost: {self.config.ghost_probability*100:.0f}% | Partial: {self.config.partial_fill_probability*100:.0f}%")
        self.log(f"Logs: {MAIN_LOG}"); self.log("=" * 80)
        self.log(""); self.log("[PREFLIGHT] Running pre-flight checks...")
        ok, checks = self.safety.preflight_check()
        for c in checks: self.log(f"  {c}")
        if ok: self.log("[PREFLIGHT] PASSED")
        else: self.log("[PREFLIGHT] FAILED"); return
        gs = self.gas.check_balance(); self.log(f"[GAS] {gs.balance_pol} POL | Floor: {self.config.gas_floor}")
        self.log(""); self.log("[RATE LIMITS] Initial budget:")
        for b in ["order", "book", "gamma", "api_key", "relayer"]: self.log(f"  {b}: {self.rate_limiter.remaining(b)}")
        for i in range(cycles):
            ok = self.run_cycle(max_sweeps=max_sweeps)
            if not ok and i < cycles - 1: self.log(f"Cycle {i+1} failed, continuing...")
        self.log(""); self.log("=" * 80); self.log("FINAL DRY RUN SUMMARY"); self.log("=" * 80)
        self.log(f"  Cycles: {self.cycle_num} | Trades: {self.total_trades} | Wins: {self.winning_trades}")
        self.log(f"  Win rate: {self.winning_trades/self.total_trades*100:.1f}%" if self.total_trades > 0 else "  Win rate: 0.0%")
        self.log(f"  Maker fills: {self.maker_fills} (zero fees) | Taker fills: {self.taker_fills}")
        self.log(f"  Resting: {self.resting_orders} | Expired: {self.expired_orders} | Partial: {self.partial_fills} | Ghost: {self.ghost_fills}")
        self.log(f"  Cumulative PnL: ${self.cumulative_pnl:.4f} | Daily PnL: ${self.daily_pnl:.4f}")
        tp = self.safety.get_true_pnl(); self.log(f"  True PnL (scoreboard): ${tp['true_pnl']:.4f}")
        self.log(f"  Total recycled: ${self.total_recycled:.2f} | Recycles: {self.recycle_count}")
        self.log(f"  Ghost fills: {self.ghost_fills} | Failed claims: {self.safety.state.total_failed_claims}")
        self.log(f"  Kill switch: {self.safety.state.is_killed} | 429s: {self.safety.state.rate_limit_429_count}/{self.config.max_429_before_trip}")
        for b in ["order", "book", "gamma", "api_key", "relayer"]: self.log(f"    {b}: {self.rate_limiter.remaining(b)} remaining")
        gs = self.gas.check_balance(); self.log(f"  Gas: {gs.balance_pol} POL | Worked markets: {len(self.safety.state.worked_markets)}")
        self.log(f"  Logs: {MAIN_LOG} | {TRADE_LOG} | {MARKET_LOG}")
        self.log("=" * 80); self.log("DRY RUN COMPLETE"); self.log("=" * 80)
        if self.errors: self.log(f"ERRORS: {len(self.errors)}"); [self.log(f"  {e}") for e in self.errors]

if __name__ == "__main__":
    bot = AdvancedDryRunner()
    bot.run(cycles=3, max_sweeps=10)
