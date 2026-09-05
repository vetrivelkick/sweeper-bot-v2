"""
Sweeper Bot V2 - Double-Entry Ledger (Phase 8)

Production hardening: Complete audit trail of all capital flows.
Every entry has balanced debit/credit pairs.

Accounts:
  USDC      - Collateral currency (asset, debit+)
  POSITION   - Conditional tokens (asset, debit+)
  GAS        - Gas costs (expense, debit+)
  FEE        - Taker fees (expense, debit+)
  INCOME     - Gross profit from merge (revenue, credit+)

PnL = INCOME - GAS - FEE (automatically calculated)

Entry types per trade lifecycle:
  1. BUY_WINNING  - Debit POSITION, Credit USDC
  2. BUY_LOSER    - Debit POSITION, Credit USDC
  3. FEE_PAID     - Debit FEE, Credit USDC (taker only)
  4. GAS_PAID     - Debit GAS, Credit USDC
  5. MERGE       - Debit USDC (recovered), Credit POSITION (cost), Credit INCOME (gross profit)

Usage:
    ledger = DoubleEntryLedger()
    ledger.record_buy_winning(trade_id, price, shares, is_maker=True)
    ledger.record_buy_loser(trade_id, price, shares)
    ledger.record_merge(trade_id, shares, winning_cost, loser_cost)
    ledger.dump()

AUDIT FIX #28: Trade history, daily PnL tracking, ledger status
SECTION 17 AUDIT: Per-trade gas/fee tracking, redemption entries, ledger persistence,
                 reconciliation with BotState, recycle entries
"""
import json, os, time, logging
from dataclasses import dataclass, field, asdict
from typing import Optional, List
from datetime import datetime, timezone

logger = logging.getLogger("sweeper.ledger")

@dataclass
class LedgerEntry:
    entry_id: int
    timestamp: str
    trade_id: int
    entry_type: str
    account: str
    debit: float
    credit: float
    balance_after: float
    description: str

@dataclass
class AccountBalance:
    USDC: float = 0.0
    POSITION: float = 0.0
    FEE: float = 0.0
    GAS: float = 0.0
    INCOME: float = 0.0

class DoubleEntryLedger:
    """Double-entry ledger for complete capital flow audit trail."""

    def __init__(self, log_dir="logs"):
        self.entries: List[LedgerEntry] = []
        self.balances = AccountBalance()
        self._next_id = 1
        self._log_dir = log_dir
        self._total_recycled = 0.0
        self._recycle_count = 0
        # AUDIT FIX #28: Trade history and daily PnL tracking
        self._trade_history: List[dict] = []
        self._max_history = 500
        self._daily_pnl: dict = {}  # date_str -> pnl
        # SECTION 17 AUDIT: Per-trade gas/fee tracking for accurate net PnL
        self._trade_gas: dict = {}
        self._trade_fee: dict = {}
        os.makedirs(log_dir, exist_ok=True)
        self._ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._ledger_file = os.path.join(log_dir, f"ledger_{self._ts}.json")
        self._balance_file = os.path.join(log_dir, f"balances_{self._ts}.json")

    def _add_entry(self, trade_id, entry_type, account, debit, credit, description):
        balance = getattr(self.balances, account, 0.0)
        balance = balance + debit - credit
        setattr(self.balances, account, balance)
        entry = LedgerEntry(
            entry_id=self._next_id,
            timestamp=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            trade_id=trade_id,
            entry_type=entry_type,
            account=account,
            debit=round(debit, 6),
            credit=round(credit, 6),
            balance_after=round(balance, 6),
            description=description,
        )
        self.entries.append(entry)
        self._next_id += 1
        return entry

    def record_buy_winning(self, trade_id, price, shares, is_maker=True):
        cost = price * shares
        self._add_entry(trade_id, "BUY_WINNING", "POSITION", cost, 0.0,
                        f"Buy {shares:.0f} winning tokens @ ${price} (cost ${cost:.4f})")
        self._add_entry(trade_id, "BUY_WINNING", "USDC", 0.0, cost,
                        f"Pay ${cost:.4f} USDC for winning tokens")
        logger.info(f"[LEDGER] Trade {trade_id}: BUY_WINNING {shares:.0f} @ ${price} = ${cost:.4f}")

    def record_buy_loser(self, trade_id, price, shares):
        cost = price * shares
        self._add_entry(trade_id, "BUY_LOSER", "POSITION", cost, 0.0,
                        f"Buy {shares:.0f} losing tokens @ ${price} (cost ${cost:.4f})")
        self._add_entry(trade_id, "BUY_LOSER", "USDC", 0.0, cost,
                        f"Pay ${cost:.4f} USDC for losing tokens")
        logger.info(f"[LEDGER] Trade {trade_id}: BUY_LOSER {shares:.0f} @ ${price} = ${cost:.4f}")

    def record_fee(self, trade_id, fee_amount, is_maker=True):
        if is_maker or fee_amount <= 0:
            return
        self._trade_fee[trade_id] = self._trade_fee.get(trade_id, 0.0) + fee_amount  # SECTION 17 AUDIT
        self._add_entry(trade_id, "FEE_PAID", "FEE", fee_amount, 0.0,
                        f"Taker fee ${fee_amount:.4f}")
        self._add_entry(trade_id, "FEE_PAID", "USDC", 0.0, fee_amount,
                        f"Pay ${fee_amount:.4f} USDC fee")
        logger.info(f"[LEDGER] Trade {trade_id}: FEE_PAID ${fee_amount:.4f}")

    def record_gas(self, trade_id, gas_cost):
        if gas_cost <= 0:
            return
        self._trade_gas[trade_id] = self._trade_gas.get(trade_id, 0.0) + gas_cost  # SECTION 17 AUDIT
        self._add_entry(trade_id, "GAS_PAID", "GAS", gas_cost, 0.0,
                        f"Gas cost ${gas_cost:.4f}")
        self._add_entry(trade_id, "GAS_PAID", "USDC", 0.0, gas_cost,
                        f"Pay ${gas_cost:.4f} USDC gas")
        logger.info(f"[LEDGER] Trade {trade_id}: GAS_PAID ${gas_cost:.4f}")

    def record_merge(self, trade_id, shares, winning_cost, loser_cost):
        """Merge YES+NO into pUSD. Balanced: Debit USDC = Credit POSITION + Credit INCOME."""
        total_position_cost = winning_cost + loser_cost
        recovered = shares * 1.0
        gross_profit = recovered - total_position_cost

        self._add_entry(trade_id, "MERGE", "POSITION", 0.0, total_position_cost,
                        f"Merge {shares:.0f} YES + {shares:.0f} NO (cost basis ${total_position_cost:.4f})")
        self._add_entry(trade_id, "MERGE", "USDC", recovered, 0.0,
                        f"Recover ${recovered:.4f} pUSD from merge")
        self._add_entry(trade_id, "MERGE", "INCOME", 0.0, gross_profit,
                        f"Gross profit from merge: ${gross_profit:.4f}")
        self._total_recycled += recovered
        self._recycle_count += 1
        # AUDIT FIX #28: Track trade history
        gas_for_trade = self._trade_gas.get(trade_id, 0.0)  # SECTION 17 AUDIT: Per-trade gas
        fee_for_trade = self._trade_fee.get(trade_id, 0.0)  # SECTION 17 AUDIT: Per-trade fee
        net_pnl = gross_profit - gas_for_trade - fee_for_trade
        self._trade_history.append({
            'trade_id': trade_id,
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'MERGE',
            'shares': shares,
            'winning_cost': round(winning_cost, 4),
            'loser_cost': round(loser_cost, 4),
            'recovered': round(recovered, 4),
            'gross_profit': round(gross_profit, 4),
            'gas_cost': round(gas_for_trade, 4),
            'fee_cost': round(fee_for_trade, 4),
            'net_pnl': round(net_pnl, 4),
        })
        if len(self._trade_history) > self._max_history:
            self._trade_history = self._trade_history[-self._max_history:]
        # AUDIT FIX #28: Track daily PnL
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0.0) + net_pnl
        logger.info(f"[LEDGER] Trade {trade_id}: MERGE {shares:.0f} pairs -> ${recovered:.4f} pUSD (profit ${gross_profit:.4f})")


    def record_redeem(self, trade_id, shares, winning_cost):
        """SECTION 17 AUDIT: Record redemption of winning tokens for USDC."""
        recovered = shares * 1.0
        gross_profit = recovered - winning_cost

        self._add_entry(trade_id, "REDEEM", "POSITION", 0.0, winning_cost,
                        f"Redeem {shares:.0f} winning tokens (cost basis ${winning_cost:.4f})")
        self._add_entry(trade_id, "REDEEM", "USDC", recovered, 0.0,
                        f"Recover ${recovered:.4f} USDC from redemption")
        self._add_entry(trade_id, "REDEEM", "INCOME", 0.0, gross_profit,
                        f"Gross profit from redemption: ${gross_profit:.4f}")
        self._total_recycled += recovered
        self._recycle_count += 1
        gas_for_trade = self._trade_gas.get(trade_id, 0.0)
        fee_for_trade = self._trade_fee.get(trade_id, 0.0)
        net_pnl = gross_profit - gas_for_trade - fee_for_trade
        self._trade_history.append({
            'trade_id': trade_id,
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'REDEEM',
            'shares': shares,
            'winning_cost': round(winning_cost, 4),
            'loser_cost': 0.0,
            'recovered': round(recovered, 4),
            'gross_profit': round(gross_profit, 4),
            'gas_cost': round(gas_for_trade, 4),
            'fee_cost': round(fee_for_trade, 4),
            'net_pnl': round(net_pnl, 4),
        })
        if len(self._trade_history) > self._max_history:
            self._trade_history = self._trade_history[-self._max_history:]
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0.0) + net_pnl
        logger.info(f"[LEDGER] Trade {trade_id}: REDEEM {shares:.0f} -> ${recovered:.4f} USDC (profit ${gross_profit:.4f})")

    def record_recycle(self, trade_id, shares, winning_cost, complementary_cost):
        """SECTION 17 AUDIT: Record complementary-token recycle (buy loser + merge)."""
        total_cost = winning_cost + complementary_cost
        recovered = shares * 1.0
        gross_profit = recovered - total_cost

        self._add_entry(trade_id, "RECYCLE", "POSITION", 0.0, total_cost,
                        f"Recycle {shares:.0f} pairs (cost ${total_cost:.4f})")
        self._add_entry(trade_id, "RECYCLE", "USDC", recovered, 0.0,
                        f"Recover ${recovered:.4f} USDC from recycle")
        self._add_entry(trade_id, "RECYCLE", "INCOME", 0.0, gross_profit,
                        f"Gross profit from recycle: ${gross_profit:.4f}")
        self._total_recycled += recovered
        self._recycle_count += 1
        gas_for_trade = self._trade_gas.get(trade_id, 0.0)
        fee_for_trade = self._trade_fee.get(trade_id, 0.0)
        net_pnl = gross_profit - gas_for_trade - fee_for_trade
        self._trade_history.append({
            'trade_id': trade_id,
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'RECYCLE',
            'shares': shares,
            'winning_cost': round(winning_cost, 4),
            'complementary_cost': round(complementary_cost, 4),
            'recovered': round(recovered, 4),
            'gross_profit': round(gross_profit, 4),
            'gas_cost': round(gas_for_trade, 4),
            'fee_cost': round(fee_for_trade, 4),
            'net_pnl': round(net_pnl, 4),
        })
        if len(self._trade_history) > self._max_history:
            self._trade_history = self._trade_history[-self._max_history:]
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0.0) + net_pnl
        logger.info(f"[LEDGER] Trade {trade_id}: RECYCLE {shares:.0f} -> ${recovered:.4f} USDC (profit ${gross_profit:.4f})")

    def record_pnl(self, trade_id, net_pnl):
        """No-op: PnL is automatically calculated as INCOME - GAS - FEE."""
        pass

    def verify_balanced(self):
        total_debit = sum(e.debit for e in self.entries)
        total_credit = sum(e.credit for e in self.entries)
        diff = abs(total_debit - total_credit)
        balanced = diff < 0.01
        return balanced, total_debit, total_credit, diff

    def get_pnl(self):
        return -self.balances.INCOME - self.balances.GAS - self.balances.FEE

    def get_summary(self):
        balanced, td, tc, diff = self.verify_balanced()
        return {
            "balances": asdict(self.balances),
            "pnl": round(self.get_pnl(), 4),
            "total_recycled": round(self._total_recycled, 4),
            "recycle_count": self._recycle_count,
            "total_entries": len(self.entries),
            "total_debits": round(td, 4),
            "total_credits": round(tc, 4),
            "balanced": balanced,
            "balance_diff": round(diff, 6),
            "unique_trades": len(set(e.trade_id for e in self.entries)),
        }

    def get_trade_history(self, limit: int = 20) -> list:
        """AUDIT FIX #28: Return recent trade history with per-trade PnL."""
        return self._trade_history[-limit:]

    def get_daily_pnl(self) -> dict:
        """AUDIT FIX #28: Return daily PnL breakdown."""
        return {k: round(v, 4) for k, v in sorted(self._daily_pnl.items())}

    def get_ledger_status(self) -> dict:
        """AUDIT FIX #28: Return ledger status for monitoring."""
        balanced, td, tc, diff = self.verify_balanced()
        return {
            'total_entries': len(self.entries),
            'unique_trades': len(set(e.trade_id for e in self.entries)),
            'total_pnl': round(self.get_pnl(), 4),
            'total_recycled': round(self._total_recycled, 4),
            'recycle_count': self._recycle_count,
            'balanced': balanced,
            'balance_diff': round(diff, 6),
            'trade_history_size': len(self._trade_history),
            'daily_pnl_entries': len(self._daily_pnl),
            'accounts': asdict(self.balances),
        }


    @classmethod
    def load(cls, ledger_file):
        """SECTION 17 AUDIT: Load ledger from JSON file for persistence across restarts."""
        ledger = cls(log_dir=os.path.dirname(ledger_file))
        try:
            with open(ledger_file, 'r') as f:
                entries = json.load(f)
            for e in entries:
                entry = LedgerEntry(**e)
                ledger.entries.append(entry)
                if entry.entry_id >= ledger._next_id:
                    ledger._next_id = entry.entry_id + 1
                balance = getattr(ledger.balances, entry.account, 0.0)
                balance = balance + entry.debit - entry.credit
                setattr(ledger.balances, entry.account, balance)
            logger.info(f"Loaded {len(ledger.entries)} entries from {ledger_file}")
        except Exception as e:
            logger.warning(f"Failed to load ledger: {e}")
        return ledger

    def reconcile_with_state(self, bot_state):
        """SECTION 17 AUDIT: Reconcile ledger PnL with BotState PnL."""
        ledger_pnl = self.get_pnl()
        state_pnl = getattr(bot_state, 'cumulative_pnl', 0.0)
        diff = abs(ledger_pnl - state_pnl)
        return {
            'ledger_pnl': round(ledger_pnl, 4),
            'state_pnl': round(state_pnl, 4),
            'difference': round(diff, 4),
            'reconciled': diff < 0.01,
        }

    def dump(self):
        with open(self._ledger_file, "w") as f:
            json.dump([asdict(e) for e in self.entries], f, indent=2)
        with open(self._balance_file, "w") as f:
            json.dump(self.get_summary(), f, indent=2)
        logger.info(f"[LEDGER] Dumped {len(self.entries)} entries to {self._ledger_file}")
        return self._ledger_file

    def log_summary(self, log_fn=print):
        s = self.get_summary()
        log_fn("=" * 60)
        log_fn("  DOUBLE-ENTRY LEDGER SUMMARY")
        log_fn("=" * 60)
        log_fn(f"  Total Entries:    {s['total_entries']}")
        log_fn(f"  Unique Trades:   {s['unique_trades']}")
        log_fn(f"  Total Debits:     ${s['total_debits']:.4f}")
        log_fn(f"  Total Credits:    ${s['total_credits']:.4f}")
        log_fn(f"  Balanced:         {s['balanced']} (diff: ${s['balance_diff']:.6f})")
        log_fn("")
        b = s['balances']
        log_fn(f"  USDC Balance:     ${b['USDC']:.4f}")
        log_fn(f"  Position Cost:    ${b['POSITION']:.4f}")
        log_fn(f"  Fees Paid:        ${b['FEE']:.4f}")
        log_fn(f"  Gas Paid:         ${b['GAS']:.4f}")
        log_fn(f"  Gross Income:     ${-b['INCOME']:.4f}")
        log_fn(f"  Net PnL:         ${s['pnl']:.4f}  (Income - Gas - Fees)")
        log_fn(f"  Total Recycled:   ${s['total_recycled']:.4f}  ({s['recycle_count']} recycles)")
        log_fn("=" * 60)
