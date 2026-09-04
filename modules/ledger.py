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
        self._add_entry(trade_id, "FEE_PAID", "FEE", fee_amount, 0.0,
                        f"Taker fee ${fee_amount:.4f}")
        self._add_entry(trade_id, "FEE_PAID", "USDC", 0.0, fee_amount,
                        f"Pay ${fee_amount:.4f} USDC fee")
        logger.info(f"[LEDGER] Trade {trade_id}: FEE_PAID ${fee_amount:.4f}")

    def record_gas(self, trade_id, gas_cost):
        if gas_cost <= 0:
            return
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
        logger.info(f"[LEDGER] Trade {trade_id}: MERGE {shares:.0f} pairs -> ${recovered:.4f} pUSD (profit ${gross_profit:.4f})")

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
        return self.balances.INCOME - self.balances.GAS - self.balances.FEE

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
        log_fn(f"  Gross Income:     ${b['INCOME']:.4f}")
        log_fn(f"  Net PnL:         ${s['pnl']:.4f}  (Income - Gas - Fees)")
        log_fn(f"  Total Recycled:   ${s['total_recycled']:.4f}  ({s['recycle_count']} recycles)")
        log_fn("=" * 60)
