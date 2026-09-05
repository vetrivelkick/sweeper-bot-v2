"""SECTION 20 AUDIT: Observability module - HTTP endpoints for metrics, health, and alerts.

Provides:
- ObservabilityServer: Lightweight HTTP server exposing:
  /metrics  - Prometheus-format metrics for scraping
  /health   - Health check endpoint (JSON)
  /ready    - Readiness check endpoint
  /alerts   - Recent alerts (JSON)
  /status   - Full bot status (JSON)
- prometheus_export: Convert MetricsCollector to Prometheus text format
- health_status: Comprehensive health status dict
- setup_log_rotation: Configure rotating file handler for logs
"""
import json, os, time, logging, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

logger = logging.getLogger("sweeper.observability")


def prometheus_export(metrics_collector) -> str:
    """Export metrics in Prometheus text exposition format."""
    lines = []
    for name, counter in metrics_collector.counters.items():
        lines.append(f"# HELP {name} {counter.help}")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {counter.get()}")
    for name, gauge in metrics_collector.gauges.items():
        lines.append(f"# HELP {name} {gauge.help}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {gauge.get()}")
    return "\n".join(lines) + "\n"


def health_status(safety, metrics=None) -> dict:
    """Get comprehensive health status."""
    health = safety.health_check() if hasattr(safety, 'health_check') else {}
    risk = safety.get_risk_status() if hasattr(safety, 'get_risk_status') else {}
    true_pnl = safety.get_true_pnl() if hasattr(safety, 'get_true_pnl') else {}
    status = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'status': health.get('status', 'unknown'),
        'is_killed': health.get('is_killed', False),
        'kill_reason': health.get('kill_reason'),
        'uptime_seconds': health.get('uptime_seconds', 0),
        'daily_pnl': health.get('daily_pnl', 0),
        'total_exposure': health.get('total_exposure', 0),
        'within_limits': health.get('within_limits', True),
        'open_positions': health.get('open_positions', 0),
        'risk_score': risk.get('risk_score', 0),
        'consecutive_losses': risk.get('consecutive_losses', 0),
        'rate_limit_429s': risk.get('rate_limit_429s', 0),
        'total_buys': true_pnl.get('total_buys', 0),
        'total_redeems': true_pnl.get('total_redeems', 0),
        'true_pnl': true_pnl.get('true_pnl', 0),
        'paper_mode': health.get('paper_mode', True),
    }
    if metrics and hasattr(metrics, 'get_alert_status'):
        status['alert_status'] = metrics.get_alert_status()
    return status


class ObservabilityHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/metrics': self._handle_metrics()
        elif path == '/health': self._handle_health()
        elif path == '/ready': self._handle_ready()
        elif path == '/alerts': self._handle_alerts()
        elif path == '/status': self._handle_status()
        elif path == '/': self._handle_root()
        else: self.send_error(404, "Not found")

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, content_type='text/plain'):
        body = text.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_metrics(self):
        if hasattr(self.server, '_metrics'):
            self._send_text(prometheus_export(self.server._metrics), content_type='text/plain; version=0.0.4; charset=utf-8')
        else:
            self._send_text("# Metrics collector not available\n")

    def _handle_health(self):
        if hasattr(self.server, '_safety'):
            status = health_status(self.server._safety, getattr(self.server, '_metrics', None))
            http_status = 200 if status['status'] == 'healthy' else 503
            self._send_json(status, http_status)
        else:
            self._send_json({'status': 'unknown'}, 503)

    def _handle_ready(self):
        if hasattr(self.server, '_safety'):
            ready = not self.server._safety.state.is_killed
            self._send_json({'ready': ready, 'killed': self.server._safety.state.is_killed}, 200 if ready else 503)
        else:
            self._send_json({'ready': False}, 503)

    def _handle_alerts(self):
        if hasattr(self.server, '_metrics') and hasattr(self.server._metrics, 'get_alert_history'):
            alerts = self.server._metrics.get_alert_history(50)
            self._send_json({'alerts': alerts, 'count': len(alerts)})
        else:
            self._send_json({'alerts': [], 'count': 0})

    def _handle_status(self):
        status = {}
        if hasattr(self.server, '_safety'):
            status = health_status(self.server._safety, getattr(self.server, '_metrics', None))
        if hasattr(self.server, '_metrics'):
            status['metrics'] = self.server._metrics.export()
        self._send_json(status)

    def _handle_root(self):
        self._send_text("Sweeper Bot V2 - Observability Endpoints\n\n  /metrics  - Prometheus-format metrics\n  /health   - Health check (JSON)\n  /ready    - Readiness check\n  /alerts   - Recent alerts (JSON)\n  /status   - Full status (JSON)\n")


class ObservabilityServer:
    """Lightweight HTTP server for observability endpoints."""
    def __init__(self, port=9090, safety=None, metrics=None, host='0.0.0.0'):
        self._port = port
        self._host = host
        self._safety = safety
        self._metrics = metrics
        self._server = None
        self._thread = None

    def start(self):
        self._server = HTTPServer((self._host, self._port), ObservabilityHandler)
        self._server._safety = self._safety
        self._server._metrics = self._metrics
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"Observability server started on {self._host}:{self._port}")

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Observability server stopped")

    def is_running(self) -> bool:
        return self._server is not None


def setup_log_rotation(log_file='logs/sweeper.log', max_bytes=10*1024*1024, backup_count=5):
    """SECTION 20 AUDIT: Configure rotating file handler for logs."""
    import logging.handlers
    os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        if isinstance(handler, logging.FileHandler) and not isinstance(handler, logging.handlers.RotatingFileHandler):
            root_logger.removeHandler(handler)
    rotating_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    rotating_handler.setFormatter(formatter)
    root_logger.addHandler(rotating_handler)
    logger.info(f"Log rotation: {log_file} (max {max_bytes/1024/1024:.0f}MB, {backup_count} backups)")
    return rotating_handler
