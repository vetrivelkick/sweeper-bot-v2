# Sweeper Bot V2 — Deployment Guide

## Quick Start
```bash
git clone https://github.com/vetrivelkick/sweeper-bot-v2.git
cd sweeper-bot-v2
pip install -r requirements.txt
cp .env.example .env  # Edit with credentials
python3 run_dry.py     # Paper trading dry run
python3 main.py --paper  # Production paper mode
python3 main.py --live    # Live trading (BLOCKED - see P0_BLOCKED in config)
```

### Docker Quick Start
```bash
docker-compose up -d    # Build and run in background
docker-compose logs -f   # Follow logs
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

## Docker Deployment (AUDIT FIX #17)

### Quick Start with Docker
```bash
# Build and run with docker-compose
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down

# Rebuild after code changes
docker-compose up -d --build
```

### Docker Health Check
The container includes a health check that runs every 60s:
- Calls `SafetyRails.health_check()` to verify bot status
- Status `healthy` or `degraded` = container healthy
- Status `killed` = container unhealthy (restarts due to `unless-stopped` policy)

### Docker Resource Limits
- Memory: 512M limit, 256M reserved
- CPU: 0.5 cores limit, 0.25 reserved
- Log rotation: 10MB max, 3 files

## Environment Variables (Complete Reference)

### Core Trading
| Variable | Default | Description |
|----------|---------|-------------|
| BUY_PRICE | 0.99 | Max buy price for winning side |
| LOSER_MAX_PRICE | 0.005 | Max buy price for losing side |
| MIN_ENTRY_PRICE | 0.985 | Minimum entry price (maker) |
| PREFER_MAKER | true | Use GTC post-only maker orders |
| ALLOW_TAKER_FALLBACK | false | Allow taker fallback |

### Risk Limits
| Variable | Default | Description |
|----------|---------|-------------|
| MAX_DAILY_LOSS | 50.0 | Max daily loss before kill switch |
| MAX_PORTFOLIO_EXPOSURE_USD | 2000 | Max total portfolio exposure |
| MAX_EVENT_EXPOSURE_USD | 500 | Max exposure per event |
| MAX_PER_MARKET_EXPOSURE | 200.0 | Max exposure per market (Fix #8) |
| MAX_429_BEFORE_TRIP | 3 | Max 429s before kill switch |

### Order Management
| Variable | Default | Description |
|----------|---------|-------------|
| RESTING_ORDER_TIMEOUT | 120 | Resting order timeout (seconds) |
| ORDER_RECONCILE_INTERVAL | 2 | Reconciliation interval (seconds) |
| CANCEL_ORDERS_ON_SHUTDOWN | true | Cancel orders on shutdown |
| TOUCH_FILL_SECONDS | 8 | Touch fill delay (seconds) |

### Paper Trading Simulation
| Variable | Default | Description |
|----------|---------|-------------|
| FILL_PROBABILITY | 0.35 | Maker fill probability |
| GHOST_PROBABILITY | 0.05 | Ghost fill probability |
| PARTIAL_FILL_PROBABILITY | 0.25 | Partial fill probability |
| PARTIAL_FILL_RATIO | 0.4 | Partial fill size ratio |

### Logging (Fix #7)
| Variable | Default | Description |
|----------|---------|-------------|
| LOG_LEVEL | INFO | Log level (DEBUG/INFO/WARNING/ERROR) |
| LOG_JSON | true | JSON structured logging (true/false) |

### Infrastructure
| Variable | Default | Description |
|----------|---------|-------------|
| POLYGON_RPC | https://polygon-rpc.com | Polygon RPC endpoint |
| GAS_FLOOR | 0.5 | Min gas balance (POL) |
| BLOCK_CONFIRMATIONS_REQUIRED | 3 | Block confirmations for settlement (Fix #6) |

## API Endpoints
- Gamma API: https://gamma-api.polymarket.com/markets
- CLOB API: https://clob.polymarket.com/book?token_id=<id>
- WebSocket: wss://ws-subscriptions-clob.polymarket.com/ws/market
