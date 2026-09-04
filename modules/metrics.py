"""
Sweeper Bot V2 - Metrics & Alerting (Phase 8)

Production hardening: Prometheus-style metrics and alerting system.
Tracks all bot operations and triggers alerts on critical conditions.

Metrics tracked:
  - trades_total, trades_won, trades_rejected
  - pnl_cumulative, pnl_daily
  - maker_fills, taker_fills, ghost_fills, expired_orders
  - kill_switch_trips, exposure_breaches
  - rate_limit_429s, rate_limit_425s
  - gas_balance_pol
  - resting_orders, reserved_collateral
  - recycle_count, total_recycled

Alerts:
  - KILL_SWITCH: Kill switch triggered
  - EXPOSURE_BREACH: Portfolio exposure exceeded
  - RATE_LIMIT_SPIKE: Multiple 429s in short window
  - GAS_LOW: Gas balance below floor
  - CONSECUTIVE_LOSS: N consecutive losing trades
  - GHOST_FILL_SPIKE: Ghost fill rate > threshold

Usage:
    metrics = MetricsCollector()
    metrics.inc("trades_total")
    metrics.set("pnl_cumulative", 6.96)
    metrics.export()
    alerts = AlertManager(config, safety)
    alerts.check_all()
"""
import json, os, time, logging
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

logger = logging.getLogger("sweeper.metrics")


class Counter:
    """Monotonically increasing counter."""
    def __init__(self, name: str, help_text: str = ""):
        self.name = name
        self.help = help_text
        self._value = 0

    def inc(self, amount: float = 1):
        self._value += amount

    def get(self) -> float:
        return self._value


class Gauge:
    """Value that can go up or down."""
    def __init__(self, name: str, help_text: str = ""):
        self.name = name
        self.help = help_text
        self._value = 0.0

    def set(self, value: float):
        self._value = value

    def inc(self, amount: float = 1):
        self._value += amount

    def dec(self, amount: float = 1):
        self._value -= amount

    def get(self) -> float:
        return self._value


class MetricsCollector:
    """Collect and export bot metrics."""

    def __init__(self, log_dir: str = "logs"):
        self.counters: Dict[str, Counter] = {}
        self.gauges: Dict[str, Gauge] = {}
        self._log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._metrics_file = os.path.join(log_dir, f"metrics_{self._ts}.json")
        self._alert_log = os.path.join(log_dir, f"alerts_{self._ts}.log")
        self._alerts: List[Dict] = []
        self._start_time = time.time()

        self._init_default_metrics()

    def _init_default_metrics(self):
        counters = [
            ("trades_total", "Total trades attempted"),
            ("trades_won", "Trades that won"),
            ("trades_rejected", "Orders rejected"),
            ("maker_fills", "Maker order fills"),
            ("taker_fills", "Taker order fills"),
            ("ghost_fills", "Ghost fills (off-chain)"),
            ("expired_orders", "Expired resting orders"),
            ("partial_fills", "Partial fills"),
            ("kill_switch_trips", "Kill switch activations"),
            ("exposure_breaches", "Exposure limit breaches"),
            ("rate_limit_429s", "HTTP 429 responses"),
            ("rate_limit_425s", "HTTP 425 responses"),
            ("recycle_count", "Capital recycle operations"),
            ("rejections_post_only", "Post-only mode rejections"),
            ("rejections_cancel_only", "Cancel-only mode rejections"),
            ("rpc_errors", "RPC connection errors"),
            ("ws_reconnects", "WebSocket reconnections"),
        ]
        for name, help_text in counters:
            self.counters[name] = Counter(name, help_text)

        gauges = [
            ("pnl_cumulative", "Cumulative PnL in USD"),
            ("pnl_daily", "Daily PnL in USD"),
            ("gas_balance_pol", "Gas balance in POL"),
            ("resting_orders", "Currently resting orders"),
            ("reserved_collateral", "Collateral reserved in resting orders"),
            ("total_exposure", "Total portfolio exposure"),
            ("total_recycled", "Total pUSD recycled"),
            ("consecutive_losses", "Consecutive losing trades"),
            ("ghost_fill_rate", "Ghost fill rate percentage"),
            ("win_rate", "Win rate percentage"),
            ("uptime_seconds", "Bot uptime in seconds"),
        ]
        for name, help_text in gauges:
            self.gauges[name] = Gauge(name, help_text)

    def inc(self, name: str, amount: float = 1):
        if name in self.counters:
            self.counters[name].inc(amount)
        elif name in self.gauges:
            self.gauges[name].inc(amount)
        else:
            logger.warning(f"Unknown metric: {name}")

    def dec(self, name: str, amount: float = 1):
        if name in self.gauges:
            self.gauges[name].dec(amount)
        else:
            logger.warning(f"Unknown gauge: {name}")

    def set(self, name: str, value: float):
        if name in self.gauges:
            self.gauges[name].set(value)
        else:
            logger.warning(f"Unknown gauge: {name}")

    def get(self, name: str) -> float:
        if name in self.counters:
            return self.counters[name].get()
        if name in self.gauges:
            return self.gauges[name].get()
        return 0.0

    def record_alert(self, alert_type: str, severity: str, message: str):
        alert = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "type": alert_type,
            "severity": severity,
            "message": message,
        }
        self._alerts.append(alert)
        if severity == "CRITICAL":
            logger.critical(f"[ALERT] {alert_type}: {message}")
        elif severity == "WARNING":
            logger.warning(f"[ALERT] {alert_type}: {message}")
        else:
            logger.info(f"[ALERT] {alert_type}: {message}")
        with open(self._alert_log, "a") as f:
            f.write(json.dumps(alert) + "\n")

    def export(self) -> dict:
        """Export all metrics to dict and write to JSON file."""
        self.set("uptime_seconds", time.time() - self._start_time)
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "counters": {n: c.get() for n, c in self.counters.items()},
            "gauges": {n: g.get() for n, g in self.gauges.items()},
            "alerts": self._alerts,
        }
        with open(self._metrics_file, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"[METRICS] Exported to {self._metrics_file}")
        return data

    def log_summary(self, log_fn=print):
        """Log human-readable metrics summary."""
        d = self.export()
        log_fn("=" * 60)
        log_fn("  METRICS SUMMARY")
        log_fn("=" * 60)
        for name, val in d["counters"].items():
            log_fn(f"  {name:<25} {val}")
        log_fn("")
        for name, val in d["gauges"].items():
            log_fn(f"  {name:<25} {val}")
        log_fn("")
        log_fn(f"  Alerts: {len(self._alerts)}")
        for a in self._alerts[-5:]:
            log_fn(f"    [{a['severity']}] {a['type']}: {a['message']}")
        log_fn("=" * 60)


class AlertManager:
    """Check conditions and trigger alerts."""

    def __init__(self, config, safety, metrics: MetricsCollector):
        self.config = config
        self.safety = safety
        self.metrics = metrics

    def check_kill_switch(self) -> bool:
        killed, reason = self.safety.check_kill_switch()
        if killed:
            self.metrics.inc("kill_switch_trips")
            self.metrics.record_alert("KILL_SWITCH", "CRITICAL", f"Kill switch active: {reason}")
            return True
        return False

    def check_exposure(self, resting_orders=None) -> bool:
        if resting_orders is None:
            resting_orders = []
        exp = self.safety.get_exposure(resting_orders)
        total = exp.get("total_exposure", 0)
        max_exp = exp.get("max_portfolio", 0)
        self.metrics.set("total_exposure", float(total))
        if float(total) > float(max_exp):
            self.metrics.inc("exposure_breaches")
            self.metrics.record_alert(
                "EXPOSURE_BREACH", "CRITICAL",
                f"Exposure ${total} exceeds max ${max_exp}"
            )
            return True
        return False

    def check_rate_limits(self) -> bool:
        count_429 = getattr(self.safety.state, "rate_limit_429_count", 0)
        max_429 = getattr(self.config, "max_429_before_trip", 3)
        self.metrics.set("rate_limit_429s" if "rate_limit_429s" in self.metrics.gauges else "consecutive_losses", count_429)
        if count_429 >= max_429:
            self.metrics.record_alert(
                "RATE_LIMIT_SPIKE", "WARNING",
                f"{count_429}/{max_429} rate limit 429s"
            )
            return True
        return False

    def check_gas(self, gas_manager) -> bool:
        gs = gas_manager.check_balance()
        self.metrics.set("gas_balance_pol", float(gs.balance_pol))
        if gs.is_low:
            self.metrics.record_alert(
                "GAS_LOW", "WARNING",
                f"Gas balance {gs.balance_pol} POL below floor {self.config.gas_floor}"
            )
            return True
        return False

    def check_consecutive_losses(self, consecutive: int) -> bool:
        self.metrics.set("consecutive_losses", consecutive)
        threshold = getattr(self.config, "consecutive_loss_limit", 5)
        if consecutive >= threshold:
            self.metrics.record_alert(
                "CONSECUTIVE_LOSS", "WARNING",
                f"{consecutive} consecutive losing trades (limit: {threshold})"
            )
            return True
        return False

    def check_ghost_rate(self, total_trades: int, ghost_count: int) -> bool:
        if total_trades > 0:
            rate = (ghost_count / total_trades) * 100
            self.metrics.set("ghost_fill_rate", rate)
            threshold = 15.0  # 15% ghost rate is concerning
            if rate > threshold:
                self.metrics.record_alert(
                    "GHOST_FILL_SPIKE", "WARNING",
                    f"Ghost fill rate {rate:.1f}% exceeds {threshold}%"
                )
                return True
        return False

    def check_all(self, resting_orders=None, gas_manager=None, consecutive_losses=0, total_trades=0, ghost_count=0) -> List[str]:
        """Run all alert checks. Returns list of triggered alert types."""
        triggered = []
        if self.check_kill_switch(): triggered.append("KILL_SWITCH")
        if self.check_exposure(resting_orders): triggered.append("EXPOSURE_BREACH")
        if self.check_rate_limits(): triggered.append("RATE_LIMIT_SPIKE")
        if gas_manager and self.check_gas(gas_manager): triggered.append("GAS_LOW")
        if self.check_consecutive_losses(consecutive_losses): triggered.append("CONSECUTIVE_LOSS")
        if self.check_ghost_rate(total_trades, ghost_count): triggered.append("GHOST_FILL_SPIKE")
        return triggered
