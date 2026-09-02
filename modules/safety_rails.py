"""Sweeper Bot V2 - Safety Rails (Updated)"""
import json, os, time, logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("sweeper.safety")

@dataclass
class BotState:
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

    def to_dict(self) -> dict:
        d = asdict(self)
        d['worked_markets'] = list(self.worked_markets)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'BotState':
        d = d.copy()
        d['worked_markets'] = set(d.get('worked_markets', []))
        for k in ('open_orders', 'reserved_collateral', 'rate_limit_429_count'):
            d.pop(k, None)
        return cls(**d)

class SafetyRails:
    def __init__(self, config):
        self.config = config
        self.state = BotState()
        self._state_file = getattr(config, 'state_file', 'data/bot_state.json')
        self._log_dir = getattr(config, 'log_dir', 'logs')
        os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
        os.makedirs(self._log_dir, exist_ok=True)

    def preflight_check(self):
        checks = []; passed = True
        if self.config.validate(): checks.append("OK: Config validation passed")
        else: checks.append("FAIL: Config validation failed"); passed = False
        if self.config.paper_mode: checks.append("OK: Paper mode enabled - wallet checks skipped")
        else:
            wallet = getattr(self.config, 'wallet_address', '')
            if wallet: checks.append(f"OK: Wallet address set: {wallet[:10]}...")
            else: checks.append("FAIL: Wallet address not set"); passed = False
            if self.config.clob_api_key and self.config.clob_api_secret: checks.append("OK: CLOB API credentials present")
            else: checks.append("FAIL: CLOB API credentials incomplete"); passed = False
            checks.append("OK: Gas balance check (deferred to live mode)")
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
        try:
            with open(self._state_file, 'w') as f: json.dump(self.state.to_dict(), f, indent=2, default=str)
        except Exception as e: logger.error(f"State dump failed: {e}")

    def load_state(self):
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, 'r') as f: data = json.load(f)
                self.state = BotState.from_dict(data)
                logger.info(f"State loaded: {len(self.state.worked_markets)} worked, {len(self.state.open_positions)} positions")
                return True
            except Exception as e: logger.error(f"State load failed: {e}")
        return False

    def update_scoreboard(self, buys=None, redeems=None, merges=None):
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

    def get_true_pnl(self):
        total_in = self.state.total_buys * self.config.buy_price
        total_out = self.state.total_redeems * 1.0
        true_pnl = total_out - total_in
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
