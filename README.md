# Sweeper Bot V2

⚠️ **NOT PRODUCTION-READY** — Paper trading only. Do not run with real funds until all P0 audit items are closed.

Polymarket arbitrage bot with GTC post-only maker orders (zero fees).

## Current Status: Paper Trading Only

This bot is under active development. Multiple P0 production-readiness items remain open.
Running in live mode (`--live`) is blocked until all P0 findings are resolved.

## All 20 Features Implemented & Verified (Paper Mode)
1. GTC Post-Only Order Method
2. Maker Pricing (bid below best ask)
3. PREFER_MAKER=True
4. Maker Fee Economics (fee=0)
5. RestingOrder dataclass
6. Order Persistence
7. Reconciliation Loop (2s)
8. Partial Fills
9. Resting Order Timeout (120s)
10. Collateral Reservation
11. Cancel on Shutdown
12. Startup Reconciliation
13. Post-Only Mode 503
14. 425 Engine Restarting
15. Cancel-Only Mode
16. Touch Fill (8s)
17. Paper Fill Probability
18. Exposure Tracking
19. Maker Fill Detection
20. WebSocket Order Updates

## Quick Start
```bash
git clone https://github.com/vetrivelkick/sweeper-bot-v2.git
cd sweeper-bot-v2
pip install -r requirements.txt
cp .env.example .env  # Fill credentials
python3 run_dry.py    # Paper dry run
python3 run_paper.py --cycles 3 --sweeps 5  # Paper trade with live data
```

**Do NOT run `python3 main.py --live` — live mode is blocked.**

See DEPLOYMENT.md for full guide.
