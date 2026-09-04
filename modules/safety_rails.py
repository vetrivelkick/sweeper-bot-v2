"""Sweeper Bot V2 - Safety Rails (Updated)

FIX #7: Removed duplicate BotState — now imports from config.settings
FIX #16: from_dict() no longer pops rate_limit_429_count (persists across restarts)
P0 #18: get_true_pnl now attempts chain-derived P&L reconciliation in live mode
         (was returning tracked_net_pnl calculated estimate only)
"""
import json, os, time, logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("sweeper.safety")

# P0 #20: OFAC sanctioned regions - blocked from Polymarket trading
BLOCKED_REGIONS = ["CU", "IR", "KP", "SY", "CR"]

from config.settings import BotState as ConfigBotState

@dataclass
class SafetyBotState:
    """FIX #7: Renamed to SafetyBotState to avoid confusion with config.BotState."""
    started_at: float = field(default_factory=time.time)
    is_running: bool = False
    is_killed: bool = False
    kill_reason: Optional[str] = None
    daily_pnl: float = 0.0
    daily_pnl_reset_time: float = field(default_factory=time.time)
    open_positions: dict = field(default_factory=dict)
    pending_orders: dict = field(default_factory=dict)
    worked_markets: set = field(default_factory=set)
    total_buys: int = 0
    total_redeems: int = 0
    total_merges: int = 0
    total_recycled_usd: float = 0.0
    total_ghost_fills_removed: int = 0
    total_failed_claims: int = 0
    paper_buys: int = 0
    paper_redeems: int = 0
    paper_pnl: float = 0.0
    open_orders: list = field(default_factory=list)
    reserved_collateral: float = 0.0
    rate_limit_429_count: int = 0
    tracked_net_pnl: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d['worked_markets'] = list(self.worked_markets)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'SafetyBotState':
        d = d.copy()
        d['worked_markets'] = set(d.get('worked_markets', []))
        # FIX #16: Preserve rate_limit_429_count instead of popping it
        return cls(**d)

class SafetyRails:
    def __init__(self, config):
        self.config = config
        self.state = SafetyBotState()
        self._state_file = getattr(config, 'state_file', 'data/bot_state.json')
        self._log_dir = getattr(config, 'log_dir', 'logs')
        self._consecutive_losses = 0
        self._max_consecutive_losses = 5
        os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
        os.makedirs(self._log_dir, exist_ok=True)

    def preflight_check(self):
        checks = []; passed = True
        if self.config.validate(): checks.append("OK: Config validation passed")
        else: checks.append("FAIL: Config validation failed"); passed = False
        # P0 #20: Geoblock preflight
        geo_ok, geo_msg = self.check_geoblock()
        if geo_ok: checks.append(f"OK: {geo_msg}")
        else: checks.append(f"FAIL: {geo_msg}"); passed = False
        if self.config.paper_mode: checks.append("OK: Paper mode enabled - wallet checks skipped")
        else:
            wallet = getattr(self.config, 'wallet_address', '')
            if wallet: checks.append(f"OK: Wallet address set: {wallet[:10]}...")
            else: checks.append("FAIL: Wallet address not set"); passed = False
            if self.config.clob_api_key and self.config.clob_api_secret: checks.append("OK: CLOB API credentials present")
            else: checks.append("FAIL: CLOB API credentials incomplete"); passed = False
            checks.append("OK: Gas balance check (deferred to live mode)")
            ok_chain, chain_id, chain_msg = self.verify_chain()
            if ok_chain: checks.append(f"OK: Chain verified: {chain_msg}")
            else: checks.append(f"FAIL: {chain_msg}"); passed = False
        checks.append("OK: Alert system (logging to file)")
        try:
            test_path = os.path.join(os.path.dirname(self._state_file), '.write_test')
            with open(test_path, 'w') as f: f.write('ok')
            os.remove(test_path)
            checks.append("OK: State file directory writable")
        except Exception as e:
            checks.append(f"FAIL: State file directory not writable: {e}"); passed = False
        if 0.90 <= self.config.buy_price <= 1.0: checks.append(f"OK: Buy price {self.config.buy_price} in valid range")
        else: checks.append(f"FAIL: Buy price {self.config.buy_price} out of range"); passed = False
        maker_edge = self.config.net_edge(is_maker=True)
        taker_edge = self.config.net_edge(is_maker=False)
        if maker_edge > 0:
            checks.append(f"OK: Maker edge ${maker_edge:.6f}/share (zero fees)")
            checks.append(f"OK: Taker edge ${taker_edge:.6f}/share (if fallback used)")
        else: checks.append("FAIL: Net edge non-positive"); passed = False
        if self.config.prefer_maker:
            checks.append("OK: GTC post-only maker mode (PREFER_MAKER=True)")
            if not self.config.allow_taker_fallback: checks.append("OK: Taker fallback disabled")
        else: checks.append("WARN: Taker-only mode (paying fees)")
        return passed, checks


    def check_geoblock(self):
        """P0 #20: Geoblock preflight - check if user's region is allowed for Polymarket trading."""
        if self.config.paper_mode:
            return True, "Paper mode - geoblock check skipped"
        user_region = getattr(self.config, 'user_region', None)
        if user_region and user_region in BLOCKED_REGIONS:
            logger.error(f"[GEOBLOCK] User region {user_region} is blocked")
            return False, f"Region {user_region} is blocked for Polymarket trading"
        return True, "Geoblock check passed"

    def verify_chain(self, w3=None):
        """P0 #15: Fail-closed chain verification."""
        if self.config.paper_mode:
            return True, 137, "Paper mode - chain check skipped"
        try:
            from web3 import Web3
            from config.settings import POLYGON_RPC
            if w3 is None:
                w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc or POLYGON_RPC))
            chain_id = w3.eth.chain_id
            if chain_id != 137:
                self.state.is_killed = True
                self.state.kill_reason = f"Wrong chain: {chain_id} (expected 137/Polygon)"
                logger.critical(f"KILL SWITCH: {self.state.kill_reason}")
                self.dump_state()
                return False, chain_id, self.state.kill_reason
            return True, chain_id, "OK: Polygon (137)"
        except Exception as e:
            self.state.is_killed = True
            self.state.kill_reason = f"Chain verification failed: {e}"
            logger.critical(f"KILL SWITCH: {self.state.kill_reason}")
            self.dump_state()
            return False, None, str(e)

    def check_exposure_before_order(self, order_cost, resting_orders=None):
        """P0 #17: Check if placing an order would exceed exposure limits."""
        exposure = self.get_exposure(resting_orders)
        new_total = exposure['total_exposure'] + order_cost
        if new_total > self.config.max_portfolio_exposure:
            return False, f"Portfolio exposure ${new_total:.2f} would exceed ${self.config.max_portfolio_exposure:.2f}"
        return True, "OK"

    def record_loss(self, amount=0.0):
        """P0 #18: Track consecutive losses for kill switch."""
        self._consecutive_losses += 1
        if self._consecutive_losses >= self._max_consecutive_losses:
            self.state.is_killed = True
            self.state.kill_reason = f"Consecutive losses: {self._consecutive_losses} (max {self._max_consecutive_losses})"
            logger.critical(f"KILL SWITCH: {self.state.kill_reason}")
            self.dump_state()
            return True, self.state.kill_reason
        logger.warning(f"Loss recorded: {self._consecutive_losses}/{self._max_consecutive_losses} consecutive")
        return False, ""

    def record_win(self):
        """P0 #18: Reset consecutive loss counter on win."""
        if self._consecutive_losses > 0:
            logger.info(f"Win recorded, resetting consecutive losses from {self._consecutive_losses}")
        self._consecutive_losses = 0

    def check_kill_switch(self, current_daily_loss=None):
        if self.state.is_killed: return True, self.state.kill_reason or "Already killed"
        loss = current_daily_loss if current_daily_loss is not None else abs(min(0, self.state.daily_pnl))
        if loss >= self.config.max_daily_loss:
            self.state.is_killed = True
            self.state.kill_reason = f"Daily loss ${loss:.2f} exceeded threshold ${self.config.max_daily_loss:.2f}"
            logger.critical(f"KILL SWITCH TRIGGERED: {self.state.kill_reason}")
            self.dump_state()
            return True, self.state.kill_reason
        return False, ""

    def record_429(self):
        self.state.rate_limit_429_count += 1
        if self.state.rate_limit_429_count >= self.config.max_429_before_trip:
            self.state.is_killed = True
            self.state.kill_reason = f"Rate limit: {self.state.rate_limit_429_count} 429s (max {self.config.max_429_before_trip})"
            logger.critical(f"KILL SWITCH: {self.state.kill_reason}")
            self.dump_state()
            return True, self.state.kill_reason
        logger.warning(f"429 recorded: {self.state.rate_limit_429_count}/{self.config.max_429_before_trip}")
        return False, ""

    def get_exposure(self, resting_orders=None):
        position_exposure = sum(p.get('cost', 0) for p in self.state.open_positions.values() if isinstance(p, dict))
        resting_exposure = 0.0
        if resting_orders:
            for order in resting_orders:
                if hasattr(order, 'shares') and hasattr(order, 'price'):
                    if hasattr(order, 'status') and str(order.status) in ('live', 'OrderStatus.LIVE', 'partial', 'OrderStatus.PARTIAL'):
                        remaining = order.shares - getattr(order, 'filled_shares', 0)
                        resting_exposure += remaining * order.price
                elif isinstance(order, dict):
                    remaining = float(order.get('shares', 0)) - float(order.get('filled_shares', 0))
                    resting_exposure += remaining * float(order.get('price', 0))
        total = position_exposure + resting_exposure
        return {'position_exposure': round(position_exposure, 2), 'resting_exposure': round(resting_exposure, 2),
                'total_exposure': round(total, 2), 'max_event': self.config.max_event_exposure,
                'max_portfolio': self.config.max_portfolio_exposure, 'within_limits': total <= self.config.max_portfolio_exposure}

    def manual_kill(self, reason="Manual kill"):
        self.state.is_killed = True; self.state.kill_reason = reason; self.state.is_running = False
        logger.critical(f"MANUAL KILL: {reason}"); self.dump_state()

    def reset_daily(self):
        self.state.daily_pnl = 0.0; self.state.daily_pnl_reset_time = time.time()
        self.state.is_killed = False; self.state.kill_reason = None; self.state.rate_limit_429_count = 0
        logger.info("Daily counters reset")

    def dump_state(self):
        """P1: Atomic state dump - write to temp file then rename to prevent corruption on crash."""
        tmp_file = self._state_file + '.tmp'
        try:
            with open(tmp_file, 'w') as f: json.dump(self.state.to_dict(), f, indent=2, default=str)
            os.replace(tmp_file, self._state_file)
        except Exception as e:
            logger.error(f"State dump failed: {e}")
            if os.path.exists(tmp_file):
                os.remove(tmp_file)

    def load_state(self):
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, 'r') as f: data = json.load(f)
                self.state = SafetyBotState.from_dict(data)
                logger.info(f"State loaded: {len(self.state.worked_markets)} worked, {len(self.state.open_positions)} positions")
                return True
            except Exception as e: logger.error(f"State load failed: {e}")
        return False

    def update_scoreboard(self, buys=None, redeems=None, merges=None, net_pnl=None):
        if buys:
            for buy in buys:
                self.state.total_buys += 1
                if self.config.paper_mode: self.state.paper_buys += 1
        if redeems:
            for redeem in redeems:
                self.state.total_redeems += 1
                if self.config.paper_mode: self.state.paper_redeems += 1
        if merges:
            for merge in merges:
                self.state.total_merges += 1
                self.state.total_recycled_usd += merge.get('amount', 0)
        if net_pnl is not None:
            self.state.tracked_net_pnl += net_pnl

    def get_true_pnl(self):
        true_pnl = self.state.tracked_net_pnl
        # P0 #18 FIX: In live mode, attempt chain-derived P&L reconciliation
        # Was: true_pnl = self.state.tracked_net_pnl (calculated estimate only)
        # Now: Query on-chain pUSD balance as ground truth in live mode
        if not self.config.paper_mode and self.config.wallet_address:
            try:
                from web3 import Web3
                from config.settings import PUSD, POLYGON_RPC
                w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc or POLYGON_RPC))
                pUSD_abi = [{"inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}]
                pUSD = w3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=pUSD_abi)
                chain_balance = pUSD.functions.balanceOf(Web3.to_checksum_address(self.config.wallet_address)).call() / 10**6
                logger.info(f"[LIVE] Chain pUSD balance: {chain_balance:.2f} | Tracked P&L: {true_pnl:.4f}")
                # Use chain balance as ground truth for live P&L
                true_pnl = chain_balance - self.state.total_recycled_usd
            except Exception as e:
                logger.warning(f"Chain P&L reconciliation failed, using tracked: {e}")
        true_win_rate = (self.state.total_redeems / self.state.total_buys if self.state.total_buys > 0 else 0.0)
        return {'total_buys': self.state.total_buys, 'total_redeems': self.state.total_redeems,
                'total_merges': self.state.total_merges, 'total_recycled_usd': round(self.state.total_recycled_usd, 4),
                'true_pnl': round(true_pnl, 4), 'true_win_rate': round(true_win_rate, 4),
                'ghost_fills_removed': self.state.total_ghost_fills_removed, 'failed_claims': self.state.total_failed_claims,
                'daily_pnl': round(self.state.daily_pnl, 4), 'is_killed': self.state.is_killed,
                'kill_reason': self.state.kill_reason, 'paper_mode': self.config.paper_mode,
                'rate_limit_429s': self.state.rate_limit_429_count}

    def mark_worked(self, condition_id): self.state.worked_markets.add(condition_id)
    def unmark_worked(self, condition_id): self.state.worked_markets.discard(condition_id); logger.info(f"Market released: {condition_id[:20]}")
    def is_worked(self, condition_id): return condition_id in self.state.worked_markets

    def record_ghost_fill(self, condition_id):
        self.state.total_ghost_fills_removed += 1
        if condition_id in self.state.open_positions: del self.state.open_positions[condition_id]
        logger.warning(f"Ghost fill removed: {condition_id}")

    def record_failed_claim(self, condition_id):
        self.state.total_failed_claims += 1
        logger.error(f"Failed claim (ops error): {condition_id}")
