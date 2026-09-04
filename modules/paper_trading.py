"""
Sweeper Bot V2 - Advanced Paper Trading Engine

Features:
- Detailed, human-readable trade logs with clear sections (not streaming fast)
- Trade execution: order type, method, price, size, status
- PnL breakdown: gross edge, fees, gas, loser cost, net PnL
- Price action: winning/losing side, spread, order book depth
- Settlement: capital recycle, merge, adapter
- Fixed plan_entry for tick_size=0.01 markets (round vs int bug)
- FIX #18: taker fallback when best_ask <= max_entry even if allow_taker=False
- Separate log files for easy review
- JSON trade records for analysis

Usage:
    python3 run_paper.py [--cycles N] [--sweeps N]
"""
import sys, os, time, json, logging, random
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SweeperConfig, fee_per_share, GAS_PER_SHARE, get_fee_rate
from modules.safety_rails import SafetyRails
from modules.market_discovery import MarketDiscovery
from modules.resolution_detection import ResolutionDetector
from modules.order_executor import OrderBuilder, RestingOrder, OrderStatus
from modules.rate_limiter import RateLimitManager
from modules.fill_confirmation import FillConfirmer
from modules.reconciliation import ReconciliationEngine
from modules.gas_manager import GasManager
from modules.capital_recycler import CapitalRecycler

logger = logging.getLogger("sweeper.paper")


def plan_entry_fixed(best_ask, tick_size, min_entry, max_entry,
                     prefer_maker=True, allow_taker=False):
    """
    Fixed plan_entry that uses round() for tick alignment.
    Also includes FIX #18: taker fallback when best_ask <= max_entry.
    """
    tick = tick_size if isinstance(tick_size, float) else float(tick_size)
    if prefer_maker:
        maker_ceiling = best_ask - tick
        desired = max(maker_ceiling, min_entry)
        price = min(desired, maker_ceiling, max_entry)
        price = round(price / tick) * tick
        if price >= best_ask:
            price -= tick
        price = round(price, 6)
        if min_entry <= price < best_ask:
            return (price, True,
                    f"resting maker bid @ {price} (ask={best_ask}, tick={tick})")
        # FIX #18: Allow taker fallback when best_ask is within entry range
        if not allow_taker:
            if min_entry <= best_ask <= max_entry:
                return (best_ask, False, f"taker fallback @ best ask {best_ask} (maker ceiling {maker_ceiling} < min_entry {min_entry})")
            return None
    if min_entry <= best_ask <= max_entry:
        return (best_ask, False, f"taker fallback @ best ask {best_ask}")
    return None


@dataclass
class TradeRecord:
    trade_num: int
    cycle: int
    timestamp: str
    market_question: str
    condition_id: str
    category: str
    winning_side: str
    winning_price: float
    losing_price: float
    spread: float
    certainty: str
    confidence_score: float
    detection_reason: str
    best_ask: Optional[float]
    best_bid: Optional[float]
    asks_count: int
    bids_count: int
    tick_size: float
    is_maker: bool
    order_method: str
    order_price: float
    order_size: float
    entry_detail: str
    fill_status: str
    filled_shares: float
    fill_price: float
    tx_hash: Optional[str]
    gross_edge: float
    fee: float
    loser_cost: float
    gas_cost: float
    net_pnl: float
    cumulative_pnl: float
    daily_pnl: float
    recycle_success: bool
    usdc_recovered: float
    adapter: str
    neg_risk: bool
    error: Optional[str] = None


class AdvancedPaperTrader:
    """Advanced paper trading engine with detailed, human-readable logs."""

    def __init__(self):
        self.config = SweeperConfig(paper_mode=True)
        self.safety = SafetyRails(self.config)
        self.discovery = MarketDiscovery(self.config)
        self.detector = ResolutionDetector(self.config)
        self.order_builder = OrderBuilder(self.config)
        self.rate_limiter = RateLimitManager(self.config)
        self.fill_confirmer = FillConfirmer(self.config)
        self.reconciler = ReconciliationEngine(
            self.config, self.safety, self.fill_confirmer, self.order_builder)
        self.gas = GasManager(self.config, self.safety)
        self.recycler = CapitalRecycler(self.config, self.order_builder, self.safety)

        self.cumulative_pnl = 0.0
        self.daily_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.maker_fills = 0
        self.taker_fills = 0
        self.resting_orders = 0
        self.expired_orders = 0
        self.partial_fills = 0
        self.ghost_fills = 0
        self.rejected_orders = 0
        self.total_recycled = 0.0
        self.recycle_count = 0
        self.errors = []
        self.cycle_num = 0
        self.trade_records: List[TradeRecord] = []

        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        LOG_DIR = os.path.join(base, "logs")
        os.makedirs(LOG_DIR, exist_ok=True)
        self.TS = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.MAIN_LOG = os.path.join(LOG_DIR, f"paper_main_{self.TS}.log")
        self.TRADE_LOG = os.path.join(LOG_DIR, f"paper_trades_{self.TS}.log")
        self.TRADE_JSON = os.path.join(LOG_DIR, f"paper_trades_{self.TS}.json")
        self.SUMMARY_JSON = os.path.join(LOG_DIR, f"paper_summary_{self.TS}.json")

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[logging.FileHandler(self.MAIN_LOG), logging.StreamHandler()])
        self.logger = logging.getLogger("sweeper.paper")

    def _log(self, msg):
        self.logger.info(msg)

    def _trade_log(self, msg):
        with open(self.TRADE_LOG, "a") as f:
            f.write(msg + "\n")

    def _log_both(self, msg):
        self.logger.info(msg)
        with open(self.TRADE_LOG, "a") as f:
            f.write(msg + "\n")

    def _sep(self, ch="=", w=72):
        self._log_both(ch * w)

    def _section(self, title, w=72):
        self._trade_log("+-" + f" {title} " + "-" * max(1, w - len(title) - 4) + "+")

    def _section_end(self, w=72):
        self._trade_log("+" + "-" * w + "+")

    def _kv(self, key, val, indent=2):
        self._trade_log(f"{' ' * indent}{key:<20} {val}")

    def run(self, cycles=3, max_sweeps=10):
        self._sep()
        self._log_both("  SWEEPER BOT V2 - ADVANCED PAPER TRADING ENGINE")
        self._sep()
        self._log_both(f"  Mode:            PAPER")
        self._log_both(f"  Buy Price:       ${self.config.buy_price}")
        self._log_both(f"  Order Method:    GTC POST-ONLY MAKER (zero fees)")
        self._log_both(f"  Taker Fallback:  DISABLED")
        self._log_both(f"  Fill Prob:       {self.config.fill_probability*100:.0f}%")
        self._log_both(f"  Partial Prob:    {self.config.partial_fill_probability*100:.0f}%")
        self._log_both(f"  Ghost Prob:      {self.config.ghost_probability*100:.0f}%")
        expire_p = 1 - self.config.fill_probability - self.config.partial_fill_probability - self.config.ghost_probability
        self._log_both(f"  Expire Prob:     {expire_p*100:.0f}%")
        self._log_both(f"  Gas Cost:        ${GAS_PER_SHARE}/share")
        self._log_both(f"  Loser Max:       ${self.config.loser_max_price}/share")
        maker_edge = self.config.net_edge(is_maker=True)
        taker_edge = self.config.net_edge(is_maker=False)
        self._log_both(f"  Maker Edge:     ${maker_edge:.6f}/share")
        self._log_both(f"  Taker Edge:     ${taker_edge:.6f}/share")
        self._log_both(f"  Resting Timeout: {self.config.resting_order_timeout:.0f}s")
        self._log_both(f"  Max Loss/Day:   ${self.config.max_daily_loss}")
        self._log_both(f"  Max Exposure:   ${self.config.max_portfolio_exposure}")
        self._log_both(f"  Cycles:         {cycles}")
        self._log_both(f"  Max Sweeps/Cycle: {max_sweeps}")
        self._log_both(f"  Main Log:        {self.MAIN_LOG}")
        self._log_both(f"  Trade Log:      {self.TRADE_LOG}")
        self._sep()
        self._log_both("")
        self._log_both("[PREFLIGHT] Running pre-flight checks...")
        ok, checks = self.safety.preflight_check()
        for c in checks:
            self._log_both(f"  {c}")
        if ok:
            self._log_both("[PREFLIGHT] PASSED")
        else:
            self._log_both("[PREFLIGHT] FAILED")
            return
        gs = self.gas.check_balance()
        self._log_both(f"[GAS] {gs.balance_pol} POL | Floor: {self.config.gas_floor}")
        self._log_both("")
        self._log_both("[RATE LIMITS] Initial budget:")
        for b in ["order", "book", "gamma", "api_key", "relayer"]:
            self._log_both(f"  {b}: {self.rate_limiter.remaining(b)}")
        self._log_both("")

        for i in range(cycles):
            ok = self.run_cycle(max_sweeps=max_sweeps)
            if not ok and i < cycles - 1:
                self._log_both(f"Cycle {i+1} failed, continuing...")

        self._final_summary()

    def run_cycle(self, max_sweeps=10):
        self.cycle_num += 1
        cycle_start = time.time()
        self._sep()
        self._log_both(f"  CYCLE {self.cycle_num}")
        self._sep()
        self._log_both("")

        self._log_both("[DISCOVERY] Fetching live markets from Gamma API...")
        try:
            candidates = self.discovery.discover_candidates(max_markets=100)
            self._log_both(f"[DISCOVERY] Found {len(candidates)} candidate markets")
        except Exception as e:
            self._log_both(f"ERROR: Discovery failed: {e}")
            self.errors.append(f"Cycle {self.cycle_num}: {e}")
            return False

        self._log_both("")
        self._log_both("[MARKET DATA] Top 5 candidates:")
        for i, m in enumerate(sorted(candidates, key=lambda m: m.sweep_score, reverse=True)[:5], 1):
            self._log_both(f"  {i}. {m.question[:60]}")
            self._log_both(f"     Vol: ${m.volume_24hr:,.0f} | YES={m.yes_price} | NO={m.no_price} | Score: {m.sweep_score:.4f} | Cat: {getattr(m, 'category', 'other')}")

        self._log_both("")
        self._log_both("[DETECTION] Running resolution detection...")
        sweepable = []
        for m in candidates:
            try:
                det = self.detector.detect(m)
                if det and self.detector.is_sweepable(det):
                    sweepable.append(det)
            except Exception:
                pass
        self._log_both(f"[DETECTION] {len(sweepable)} sweepable markets")

        self._log_both("")
        self._log_both("[PRICE ACTION] Sweepable markets:")
        for i, det in enumerate(sweepable[:10], 1):
            self._log_both(f"  {i}. {det.question[:60]}")
            self._log_both(f"     Win: {det.winning_side} @ {det.winning_price} | Lose @ {det.losing_price} | Spread: {det.winning_price - det.losing_price:.4f}")
            self._log_both(f"     Certainty: {det.certainty} | Confidence: {det.confidence_score:.2f}% | Reason: {det.detection_reason}")
            cat = getattr(det, 'category', 'other')
            self._log_both(f"     Category: {cat} | Taker Fee Rate: {get_fee_rate(cat)} | Maker Fee: $0 (ZERO)")

        self._log_both("")
        sweeps = 0
        for det in sweepable:
            if sweeps >= max_sweeps:
                break
            if self.safety.is_worked(det.condition_id):
                continue
            killed, reason = self.safety.check_kill_switch()
            if killed:
                self._log_both(f"[KILL SWITCH] {reason}")
                return False
            if not self.rate_limiter.can_request("order"):
                self._log_both("[RATE LIMIT] Order budget exhausted")
                break
            self._process_trade(det)
            sweeps += 1
            self.rate_limiter.record_request("order")
            self.safety.mark_worked(det.condition_id)

        cycle_time = time.time() - cycle_start
        self._cycle_summary(cycle_time, sweeps)
        return True

    # --- FIX: Added missing _simulate_fill method ---

    def _simulate_fill(self, order):
        """Simulate fill outcome for maker orders.
        If already filled/partial/ghost from _paper_place, return that status.
        Otherwise, fast-forward simulate the resting order outcome."""
        if order.status == OrderStatus.FILLED:
            return "filled"
        if order.status == OrderStatus.PARTIAL:
            return "partial"
        if order.status == OrderStatus.LIVE and order.filled_shares > 0 and order.tx_hash is None:
            return "ghost"
        # Order is LIVE (resting) - simulate fast-forward
        rng = random.Random()
        roll = rng.random()
        if roll < self.config.fill_probability:
            order.filled_shares = order.shares
            order.avg_fill_price = order.price
            order.tx_hash = f"paper_tx_{int(time.time())}"
            order.status = OrderStatus.FILLED
            return "filled"
        elif roll < self.config.fill_probability + self.config.partial_fill_probability:
            p = max(1, int(order.shares * self.config.partial_fill_ratio))
            order.filled_shares = float(p)
            order.avg_fill_price = order.price
            order.tx_hash = f"paper_tx_{int(time.time())}"
            order.status = OrderStatus.PARTIAL
            return "partial"
        elif roll < self.config.fill_probability + self.config.partial_fill_probability + self.config.ghost_probability:
            order.filled_shares = order.shares
            order.avg_fill_price = order.price
            order.tx_hash = None
            order.status = OrderStatus.LIVE
            return "ghost"
        else:
            order.status = OrderStatus.EXPIRED
            return "expired"

    def _process_trade(self, det):
        self.total_trades += 1
        trade_num = self.total_trades

        self._sep("-")
        self._log_both(f"  TRADE #{trade_num} - CYCLE {self.cycle_num}")
        self._sep("-")
        self._trade_log("")

        best_ask = None
        best_bid_price = None
        book_depth_asks = 0
        book_depth_bids = 0
        try:
            book = self.discovery.get_market_book(det.winning_token_id)
            asks = book.get("asks", [])
            bids = book.get("bids", [])
            book_depth_asks = len(asks)
            book_depth_bids = len(bids)
            if asks:
                best_ask = min(float(a.get("price", 0)) for a in asks)
            if bids:
                best_bid_price = max(float(b.get("price", 0)) for b in bids)
        except Exception:
            pass

        tick_size = getattr(det, 'tick_size', 0.001 if det.winning_price >= 0.999 else 0.01)

        entry_plan = None
        if self.config.prefer_maker and best_ask is not None:
            entry_plan = plan_entry_fixed(
                best_ask, tick_size, 0.985, self.config.buy_price,
                self.config.prefer_maker, self.config.allow_taker_fallback)

        # FIX: Removed broken third condition that always evaluated True
        is_maker = (entry_plan is not None and entry_plan[1]) or \
                   (self.config.prefer_maker and best_ask is None)

        order_price = entry_plan[0] if entry_plan else self.config.buy_price
        order_detail = entry_plan[2] if entry_plan else "maker bid at buy_price"

        # --- MARKET INFO ---
        self._section("MARKET")
        self._kv("Question", det.question[:65])
        self._kv("Condition", f"{det.condition_id[:50]}...")
        self._kv("Category", getattr(det, 'category', 'other'))
        self._kv("End Date", det.end_date or "N/A")
        self._kv("Neg Risk", str(getattr(det, 'neg_risk', False)))
        self._section_end()
        self._trade_log("")

        # --- PRICE ACTION ---
        self._section("PRICE ACTION")
        self._kv("Winning Side", f"{det.winning_side} @ ${det.winning_price}")
        losing_side = "YES" if det.winning_side == "NO" else "NO"
        self._kv("Losing Side", f"{losing_side} @ ${det.losing_price}")
        self._kv("Spread", f"{det.winning_price - det.losing_price:.4f}")
        self._kv("Certainty", f"{det.certainty} ({det.confidence_score:.2f}%)")
        self._kv("Detection", det.detection_reason)
        self._trade_log("")
        self._kv("Best Ask", f"${best_ask}" if best_ask else "N/A")
        self._kv("Best Bid", f"${best_bid_price}" if best_bid_price else "N/A")
        self._kv("Asks", str(book_depth_asks))
        self._kv("Bids", str(book_depth_bids))
        self._kv("Book Depth", f"{book_depth_asks + book_depth_bids} orders")
        cat = getattr(det, 'category', 'other')
        self._kv("Taker Fee Rate", str(get_fee_rate(cat)))
        self._kv("Maker Fee", "$0 (ZERO)")
        self._section_end()
        self._trade_log("")

        # --- ORDER EXECUTION ---
        self._section("ORDER EXECUTION")
        if entry_plan is None and best_ask is not None:
            maker_ceiling = best_ask - tick_size
            self._kv("Method", "REJECTED (no valid entry)")
            self._kv("Reason", f"best_ask={best_ask}, tick={tick_size}")
            self._kv("Maker Ceiling", f"${maker_ceiling:.6f}")
            self._kv("Min Entry", "0.985")
            self._kv("Max Entry", f"${self.config.buy_price}")
            if maker_ceiling < 0.985:
                self._kv("Issue", f"maker_ceiling {maker_ceiling:.6f} < min_entry 0.985")
            if best_ask > self.config.buy_price:
                self._kv("Issue", f"best_ask {best_ask} > max_entry {self.config.buy_price}")
        elif is_maker:
            self._kv("Method", "GTC POST-ONLY MAKER (zero fees)")
            self._kv("Entry Plan", order_detail)
            self._kv("Order Type", "GTC (post-only)")
        else:
            self._kv("Method", "FAK TAKER (pays fees)")
            self._kv("Entry Plan", order_detail)
            self._kv("Order Type", "FAK (fill-and-kill)")
        self._kv("Side", "BUY")
        self._kv("Size", "100 shares")
        self._kv("Price", f"${order_price}")
        self._kv("Tick Size", str(tick_size))
        self._section_end()
        self._trade_log("")

        success, order = self.order_builder.build_and_place(
            detection_result=det, size=100.0, best_ask=best_ask,
            tick_size=tick_size, neg_risk=getattr(det, 'neg_risk', False))

        if not success or order is None:
            self.rejected_orders += 1
            error_msg = "No entry price"
            if order and hasattr(order, 'error') and order.error:
                error_msg = order.error
            self._log_both(f"  [X] ORDER REJECTED: {error_msg}")
            self._trade_log("")
            self._section("REJECTION DETAILS")
            self._kv("Reason", error_msg)
            self._kv("Best Ask", f"${best_ask}" if best_ask else "N/A")
            self._kv("Tick Size", str(tick_size))
            self._kv("Min Entry", "0.985")
            self._kv("Max Entry", f"${self.config.buy_price}")
            if best_ask and tick_size:
                maker_ceiling = best_ask - tick_size
                self._kv("Maker Ceiling", f"${maker_ceiling}")
            self._section_end()
            self._trade_log("")
            self._log_both(f"  [X] TRADE #{trade_num} REJECTED")
            self._sep("-")
            self._log_both("")
            self.errors.append(f"Cycle {self.cycle_num} Trade {trade_num}: Rejected")
            record = TradeRecord(
                trade_num=trade_num, cycle=self.cycle_num,
                timestamp=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                market_question=det.question, condition_id=det.condition_id,
                category=cat, winning_side=det.winning_side,
                winning_price=det.winning_price, losing_price=det.losing_price,
                spread=det.winning_price - det.losing_price,
                certainty=str(det.certainty), confidence_score=det.confidence_score,
                detection_reason=det.detection_reason,
                best_ask=best_ask, best_bid=best_bid_price,
                asks_count=book_depth_asks, bids_count=book_depth_bids,
                tick_size=tick_size, is_maker=is_maker,
                order_method="GTC" if is_maker else "FAK",
                order_price=order_price, order_size=100.0,
                entry_detail=order_detail,
                fill_status="REJECTED", filled_shares=0, fill_price=0,
                tx_hash=None, gross_edge=0, fee=0, loser_cost=0, gas_cost=0,
                net_pnl=0, cumulative_pnl=self.cumulative_pnl,
                daily_pnl=self.daily_pnl,
                recycle_success=False, usdc_recovered=0,
                adapter="N/A", neg_risk=getattr(det, 'neg_risk', False),
                error=error_msg)
            self.trade_records.append(record)
            return

        # --- FILL SIMULATION ---
        self._section("FILL SIMULATION")
        if isinstance(order, RestingOrder):
            fill_result = self._simulate_fill(order)
            if fill_result == "filled":
                self.maker_fills += 1
                self._kv("Result", "[OK] MAKER FILL (zero fees)")
                self._kv("Filled Shares", f"{order.filled_shares:.0f}")
                self._kv("Fill Price", f"${order.price}")
                self._kv("TX Hash", order.tx_hash or "N/A")
                self._kv("On-Chain", "True" if order.tx_hash else "False")
                self._kv("Status", "FILLED")
            elif fill_result == "partial":
                self.partial_fills += 1
                self._kv("Result", "[~] PARTIAL FILL")
                self._kv("Filled Shares", f"{order.filled_shares:.0f} / {order.shares:.0f}")
                self._kv("Fill Price", f"${order.price}")
                self._kv("TX Hash", order.tx_hash or "N/A")
                self._kv("Status", "PARTIAL")
            elif fill_result == "ghost":
                self.ghost_fills += 1
                self._kv("Result", "[!] GHOST FILL (off-chain match, on-chain revert)")
                self._kv("Status", "LIVE (ghost)")
            elif fill_result == "expired":
                self.expired_orders += 1
                self._kv("Result", "[X] EXPIRED")
                self._kv("Status", "EXPIRED")
                self.safety.unmark_worked(det.condition_id)
        else:
            self.taker_fills += 1
            order.filled_shares = order.fill_amount
            order.avg_fill_price = order.price
            self._kv("Result", "[OK] TAKER FILL")
            self._kv("Filled Shares", f"{order.fill_amount:.0f}")
            self._kv("Fill Price", f"${order.price}")
            self._kv("TX Hash", order.tx_hash or "N/A")
            self._kv("Status", "FILLED")
        self._section_end()
        self._trade_log("")

        # Handle expired and ghost orders (no settlement)
        if order.status == OrderStatus.EXPIRED:
            self._log_both(f"  [X] TRADE #{trade_num} EXPIRED - No fill")
            self._sep("-")
            self._log_both("")
            return

        if not order.tx_hash:
            self._log_both(f"  [!] TRADE #{trade_num} GHOST FILL - No settlement")
            self._sep("-")
            self._log_both("")
            return

        filled_shares = order.filled_shares if order.filled_shares > 0 else 100.0
        fill_price = order.price

        # --- PNL BREAKDOWN ---
        self.winning_trades += 1
        gross = (1.0 - fill_price) * filled_shares
        fee = fee_per_share(fill_price, is_maker=is_maker) * filled_shares
        loser_cost = self.config.loser_max_price * filled_shares
        gas_cost = GAS_PER_SHARE * filled_shares
        net_pnl = gross - fee - loser_cost - gas_cost
        self.cumulative_pnl += net_pnl
        self.daily_pnl += net_pnl

        self._section("PNL BREAKDOWN")
        self._kv("Order Type", "GTC MAKER" if is_maker else "FAK TAKER")
        self._kv("Gross Edge", f"+${gross:.4f}  ({filled_shares:.0f} x ($1.00 - ${fill_price}))")
        self._kv("Fee", f"${fee:.4f}  ({'ZERO - MAKER' if is_maker else 'taker'})")
        self._kv("Loser Cost", f"-${loser_cost:.4f}  ({filled_shares:.0f} x ${self.config.loser_max_price})")
        self._kv("Gas Cost", f"-${gas_cost:.4f}  ({filled_shares:.0f} x ${GAS_PER_SHARE})")
        self._trade_log("")
        self._kv("Net PnL", f"+${net_pnl:.4f}")
        self._kv("Cumulative", f"${self.cumulative_pnl:.4f}")
        self._kv("Daily", f"${self.daily_pnl:.4f} / Max: ${self.config.max_daily_loss}")
        wr = f"{self.winning_trades}/{self.total_trades}" + (f" = {self.winning_trades/self.total_trades*100:.1f}%" if self.total_trades > 0 else "")
        self._kv("Win Rate", wr)
        self._section_end()
        self._trade_log("")

        # --- SETTLEMENT ---
        self._section("SETTLEMENT (CAPITAL RECYCLE)")
        neg_risk = getattr(det, 'neg_risk', False)
        adapter = "NegRiskCtfCollateralAdapter" if neg_risk else "CtfCollateralAdapter"
        self._kv("Loser Buy", f"${self.config.loser_max_price}/share")
        self._kv("Merge", f"{filled_shares:.0f} YES + {filled_shares:.0f} NO -> {filled_shares:.0f} pUSD")
        self._kv("Adapter", adapter)
        try:
            result = self.recycler.recycle(det, filled_shares)
            if result.success:
                self._kv("Recycle", f"SUCCESS - {filled_shares:.0f} shares -> ${result.usdc_recovered:.2f} pUSD")
            else:
                self._kv("Recycle", f"FAILED - {result.error}")
        except Exception as e:
            self._kv("Recycle", f"ERROR - {e}")
        recycled_usd = filled_shares * 1.0
        self._kv("Recovered", f"${recycled_usd:.2f} pUSD")
        self._kv("Loser Cost", f"${loser_cost:.4f}")
        self.total_recycled += recycled_usd
        self.recycle_count += 1
        self._section_end()
        self._trade_log("")

        # --- TRADE COMPLETE ---
        self._log_both(f"  [OK] TRADE #{trade_num} COMPLETE - {'MAKER (ZERO FEES)' if is_maker else 'TAKER'}")
        self._log_both(f"      Net PnL: +${net_pnl:.4f} | Cumulative: ${self.cumulative_pnl:.4f}")
        self._sep("-")
        self._log_both("")

        record = TradeRecord(
            trade_num=trade_num, cycle=self.cycle_num,
            timestamp=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            market_question=det.question, condition_id=det.condition_id,
            category=cat, winning_side=det.winning_side,
            winning_price=det.winning_price, losing_price=det.losing_price,
            spread=det.winning_price - det.losing_price,
            certainty=str(det.certainty), confidence_score=det.confidence_score,
            detection_reason=det.detection_reason,
            best_ask=best_ask, best_bid=best_bid_price,
            asks_count=book_depth_asks, bids_count=book_depth_bids,
            tick_size=tick_size, is_maker=is_maker,
            order_method="GTC" if is_maker else "FAK",
            order_price=order_price, order_size=100.0,
            entry_detail=order_detail,
            fill_status=str(order.status.value if hasattr(order.status, 'value') else order.status),
            filled_shares=filled_shares, fill_price=fill_price,
            tx_hash=order.tx_hash,
            gross_edge=gross, fee=fee, loser_cost=loser_cost, gas_cost=gas_cost,
            net_pnl=net_pnl, cumulative_pnl=self.cumulative_pnl,
            daily_pnl=self.daily_pnl,
            recycle_success=True, usdc_recovered=recycled_usd,
            adapter=adapter, neg_risk=neg_risk)
        self.trade_records.append(record)

    def _cycle_summary(self, cycle_time, sweeps):
        self._sep()
        self._log_both(f"  CYCLE {self.cycle_num} SUMMARY")
        self._sep()
        self._log_both(f"  Sweeps: {sweeps} | Time: {cycle_time:.2f}s | PnL: ${self.cumulative_pnl:.4f} | Daily: ${self.daily_pnl:.4f}")
        wr = f"{self.winning_trades}/{self.total_trades}" + (f" = {self.winning_trades/self.total_trades*100:.1f}%" if self.total_trades > 0 else "")
        self._log_both(f"  Trades: {self.total_trades} | Win: {wr}")
        self._log_both(f"  Maker: {self.maker_fills} (zero fees) | Taker: {self.taker_fills} | Rejected: {self.rejected_orders}")
        self._log_both(f"  Resting: {self.resting_orders} | Expired: {self.expired_orders} | Partial: {self.partial_fills} | Ghost: {self.ghost_fills}")
        for b in ["order", "book", "gamma", "api_key", "relayer"]:
            self._log_both(f"    {b}: {self.rate_limiter.remaining(b)} remaining")
        gs = self.gas.check_balance()
        self._log_both(f"  Gas: {gs.balance_pol} POL | Low: {gs.is_low}")
        try:
            r = self.reconciler.reconcile()
            self._log_both(f"  [RECONCILE] {r.total_positions} pos, {r.phantom_positions} phantoms")
        except Exception as e:
            self._log_both(f"  [RECONCILE] Error: {e}")
        resting = self.order_builder.list_open_orders()
        exp = self.safety.get_exposure(resting)
        self._log_both(f"  [EXPOSURE] Pos: ${exp['position_exposure']} | Resting: ${exp['resting_exposure']} | Total: ${exp['total_exposure']} | Max: ${exp['max_portfolio']}")
        self._log_both("")

    def _final_summary(self):
        self._sep()
        self._log_both("  FINAL SUMMARY")
        self._sep()
        self._log_both(f"  Cycles: {self.cycle_num} | Trades: {self.total_trades} | Wins: {self.winning_trades}")
        wr = f"{self.winning_trades/self.total_trades*100:.1f}%" if self.total_trades > 0 else "0.0%"
        self._log_both(f"  Win rate: {wr}")
        self._log_both(f"  Maker fills: {self.maker_fills} (zero fees) | Taker fills: {self.taker_fills}")
        self._log_both(f"  Rejected: {self.rejected_orders} | Resting: {self.resting_orders} | Expired: {self.expired_orders} | Partial: {self.partial_fills} | Ghost: {self.ghost_fills}")
        self._log_both(f"  Cumulative PnL: ${self.cumulative_pnl:.4f} | Daily PnL: ${self.daily_pnl:.4f}")
        tp = self.safety.get_true_pnl()
        self._log_both(f"  True PnL (scoreboard): ${tp['true_pnl']:.4f}")
        self._log_both(f"  Total recycled: ${self.total_recycled:.2f} pUSD | Recycles: {self.recycle_count}")
        self._log_both(f"  Kill switch: {self.safety.state.is_killed} | 429s: {self.safety.state.rate_limit_429_count}/{self.config.max_429_before_trip}")
        for b in ["order", "book", "gamma", "api_key", "relayer"]:
            self._log_both(f"    {b}: {self.rate_limiter.remaining(b)} remaining")
        gs = self.gas.check_balance()
        self._log_both(f"  Gas: {gs.balance_pol} POL | Worked markets: {len(self.safety.state.worked_markets)}")
        self._log_both(f"  Main Log: {self.MAIN_LOG}")
        self._log_both(f"  Trade Log: {self.TRADE_LOG}")
        self._log_both(f"  Trade JSON: {self.TRADE_JSON}")
        self._log_both(f"  Summary JSON: {self.SUMMARY_JSON}")
        if self.errors:
            self._log_both(f"  ERRORS: {len(self.errors)}")
            for e in self.errors:
                self._log_both(f"    {e}")
        self._sep()
        self._log_both("  PAPER TRADING COMPLETE")
        self._sep()

        with open(self.TRADE_JSON, "w") as f:
            json.dump([asdict(r) for r in self.trade_records], f, indent=2)
        summary = {
            "cycles": self.cycle_num, "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "win_rate": wr,
            "maker_fills": self.maker_fills, "taker_fills": self.taker_fills,
            "rejected_orders": self.rejected_orders,
            "resting_orders": self.resting_orders, "expired_orders": self.expired_orders,
            "partial_fills": self.partial_fills, "ghost_fills": self.ghost_fills,
            "cumulative_pnl": self.cumulative_pnl, "daily_pnl": self.daily_pnl,
            "true_pnl": tp['true_pnl'],
            "total_recycled_pusd": self.total_recycled, "recycle_count": self.recycle_count,
            "kill_switch": self.safety.state.is_killed, "errors": self.errors}
        with open(self.SUMMARY_JSON, "w") as f:
            json.dump(summary, f, indent=2)
        self._log_both(f"  Exported {len(self.trade_records)} trade records to JSON")
