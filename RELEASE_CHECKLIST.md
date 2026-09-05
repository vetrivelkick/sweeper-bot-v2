# Sweeper Bot V2 - Release Checklist (Section 22)

## Pre-Release Security Audit
- [x] P0_BLOCKED = True (live mode blocked until release gate passes)
- [x] Two-factor live mode (LIVE_MODE env var + --live flag)
- [x] Process lock (fcntl) prevents duplicate instances
- [x] Kill switch (daily loss limit $50, manual kill)
- [x] Circuit breaker (300s cooldown, auto-trigger)
- [x] Rate limiting (429 count, max 3 before trip)
- [x] Exposure limits (portfolio $2000, event $500, per-market $200)
- [x] Gas floor (0.5 POL minimum)
- [x] Cancel on shutdown (all resting orders cancelled)
- [x] Structured JSON logging with correlation IDs
- [x] Observability server (Prometheus metrics, health, ready, alerts, status)
- [x] Double-entry ledger (balanced, PnL tracking)
- [x] State persistence (atomic save/load, worked_markets, open_positions)
- [x] Startup recovery (remote order/position recovery)
- [x] Economics gate (buy price, min size, fee check)
- [x] Order construction (GTC post-only, maker preference, no taker fallback)
- [x] Fill confirmation module
- [x] Reconciliation (position and order reconciliation)
- [x] Capital recycling (complementary token recycle)
- [x] Redemption manager (wait & verify)
- [x] WebSocket client (Base/Market/User, auto-reconnect)
- [x] Stress test (5% price drop + 50% gas spike, would survive)

## Testing
- [x] 79 pytest tests pass (config, safety, economics, ledger, rate limiter, integration, observability)
- [x] Paper trading with live Gamma API data (PnL $0.40, ROI 0.40%)
- [x] Ledger balanced (debit = credit)
- [x] Risk score 0.0, within limits
- [x] Stress test passed

## Deployment
- [x] CI workflow (pytest, synthetic, chaos, paper trading, lint)
- [x] Dockerfile (Python 3.13-slim, EXPOSE 9090, HEALTHCHECK)
- [x] Docker Compose (resource limits, log rotation, health check)
- [x] Observability server integrated into main.py
- [x] Metrics collector integrated into main.py
- [x] Log rotation configured

## Go-Live Steps
1. Set P0_BLOCKED = False in main.py
2. Set LIVE_MODE=true environment variable
3. Configure PRIVATE_KEY, WALLET_ADDRESS, CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE
4. Start with small canary (MAX_CANARY_FUNDED_USD=50)
5. Monitor /health and /metrics endpoints
6. Watch for alerts (kill switch, exposure, rate limits, gas, consecutive losses)
7. Verify ledger balance after each trade cycle
8. Run stress test periodically
