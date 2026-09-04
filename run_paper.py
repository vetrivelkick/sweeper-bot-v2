"""
Sweeper Bot V2 - Advanced Paper Trading Runner

Usage:
    python3 run_paper.py [--cycles N] [--sweeps N]

Features:
- Detailed, human-readable trade logs with clear sections
- Trade execution, PnL, price action, and settlement details
- Fixed plan_entry for tick_size=0.01 markets (round vs int bug)
- Separate log files for easy review:
    logs/paper_main_*.log      - main log (streaming)
    logs/paper_trades_*.log   - detailed trade log (human-readable)
    logs/paper_trades_*.json   - JSON trade records
    logs/paper_summary_*.json - JSON summary
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.paper_trading import AdvancedPaperTrader

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sweeper Bot V2 - Advanced Paper Trading")
    parser.add_argument("--cycles", type=int, default=3, help="Number of cycles (default: 3)")
    parser.add_argument("--sweeps", type=int, default=10, help="Max sweeps per cycle (default: 10)")
    args = parser.parse_args()

    print()
    print("=" * 72)
    print("  SWEEPER BOT V2 - ADVANCED PAPER TRADING ENGINE")
    print("  Fixed: plan_entry round() bug | Detailed logs | Settlement tracking")
    print("=" * 72)
    print()

    trader = AdvancedPaperTrader()
    trader.run(cycles=args.cycles, max_sweeps=args.sweeps)
