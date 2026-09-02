# Sweeper Bot V2 — Deployment Guide

## Quick Start
```bash
git clone https://github.com/vetrivelkick/sweeper-bot-v2.git
cd sweeper-bot-v2
pip install -r requirements.txt
cp .env.example .env  # Edit with credentials
python3 run_dry.py     # Paper trading dry run
python3 main.py --paper  # Production paper mode
python3 main.py --live    # Live trading
```

## All 20 Implemented Features
### CRITICAL (1-4): GTC Post-Only, Maker Pricing, PREFER_MAKER, Maker Fee Economics
### HIGH (5-12): RestingOrder, Order Persistence, Reconciliation Loop, Partial Fills, Timeout, Collateral Reservation, Cancel on Shutdown, Startup Reconciliation
### MEDIUM (13-17): Post-Only Mode 503, 425 Handling, Cancel-Only Mode, Touch Fill, Paper Fill Probability
### LOWER (18-20): Exposure Tracking, Maker Fill Detection, WebSocket Order Updates

## Dry Run Results (4 Runs)
| Run | Trades | Win | Maker | Partial | Expired | Ghost | PnL | Recycled |
|-----|--------|-----|-------|---------|---------|-------|-----|----------|
| 1   | 18     | 100%| 13    | 5       | 3       | 0     | $6  | $1,500  |
| 2   | 17     | 100%| 12    | 5       | 4       | 0     | $5.6| $1,400  |
| 3   | 17     | 100%| 12    | 5       | 4       | 0     | $5.6| $1,400  |
| 4   | 16     | 100%| 15    | 1       | 3       | 2     | $6.2| $1,540  |

All runs against LIVE Polymarket Gamma API. Every fill = MAKER (zero fees).

## Strategy Parameters (DO NOT CHANGE)
- Buy price: 0.99 | Loser price: <=$0.005 | Maker edge: $0.0049/share
- Max daily loss: $50 | Gas floor: 0.5 POL
- prefer_maker=True | allow_taker_fallback=False
- resting_order_timeout=120s | order_reconcile_interval=2s | cancel_orders_on_shutdown=True
- fill_probability=0.35 | ghost_probability=0.05 | partial_fill_probability=0.25
- max_event_exposure=$500 | max_portfolio_exposure=$2000 | max_429_before_trip=3

## Contract Addresses (Polygon, chain_id=137)
- pUSD: 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
- CTF: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
- CtfCollateralAdapter: 0xAdA100Db00Ca00073811820692005400218FcE1f
- NegRiskCtfCollateralAdapter: 0xadA2005600Dec949baf300f4C6120000bDB6eAab

## API Endpoints
- Gamma API: https://gamma-api.polymarket.com/markets
- CLOB API: https://clob.polymarket.com/book?token_id=<id>
- WebSocket: wss://ws-subscriptions-clob.polymarket.com/ws/market
