"""SECTION 20 AUDIT: Unit tests for observability module."""
import pytest, time, json, urllib.request, urllib.error, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import SweeperConfig
from modules.safety_rails import SafetyRails
from modules.metrics import MetricsCollector
from modules.observability import prometheus_export, health_status, ObservabilityServer, setup_log_rotation


class TestPrometheusExport:
    def test_export_counters(self):
        m = MetricsCollector()
        m.inc("trades_total", 5)
        text = prometheus_export(m)
        assert "trades_total" in text and "# TYPE trades_total counter" in text and "trades_total 5" in text

    def test_export_gauges(self):
        m = MetricsCollector()
        m.set("pnl_cumulative", 3.50)
        text = prometheus_export(m)
        assert "pnl_cumulative" in text and "# TYPE pnl_cumulative gauge" in text

    def test_export_all(self):
        m = MetricsCollector()
        m.inc("trades_total", 10)
        m.set("pnl_daily", 2.0)
        text = prometheus_export(m)
        assert "# HELP" in text and "# TYPE" in text
        assert len(text.strip().split("\n")) > 10


class TestHealthStatus:
    def test_healthy(self):
        s = health_status(SafetyRails(SweeperConfig(paper_mode=True)))
        assert s['status'] == 'healthy' and not s['is_killed']

    def test_killed(self):
        safety = SafetyRails(SweeperConfig(paper_mode=True))
        safety.state.is_killed = True
        safety.state.kill_reason = "Test"
        assert health_status(safety)['status'] == 'killed'

    def test_with_metrics(self):
        s = health_status(SafetyRails(SweeperConfig(paper_mode=True)), MetricsCollector())
        assert 'alert_status' in s


class TestObservabilityServer:
    def test_start_stop(self):
        srv = ObservabilityServer(port=19090, safety=SafetyRails(SweeperConfig(paper_mode=True)), metrics=MetricsCollector())
        srv.start()
        assert srv.is_running()
        time.sleep(0.1)
        srv.stop()
        assert not srv.is_running()

    def test_metrics_endpoint(self):
        m = MetricsCollector(); m.inc("trades_total", 3)
        srv = ObservabilityServer(port=19091, safety=SafetyRails(SweeperConfig(paper_mode=True)), metrics=m)
        srv.start(); time.sleep(0.1)
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:19091/metrics")
            assert "trades_total" in resp.read().decode() and resp.status == 200
        finally:
            srv.stop()

    def test_health_endpoint(self):
        srv = ObservabilityServer(port=19092, safety=SafetyRails(SweeperConfig(paper_mode=True)), metrics=MetricsCollector())
        srv.start(); time.sleep(0.1)
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:19092/health")
            data = json.loads(resp.read().decode())
            assert data['status'] == 'healthy' and resp.status == 200
        finally:
            srv.stop()

    def test_ready_endpoint(self):
        srv = ObservabilityServer(port=19093, safety=SafetyRails(SweeperConfig(paper_mode=True)), metrics=MetricsCollector())
        srv.start(); time.sleep(0.1)
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:19093/ready")
            assert json.loads(resp.read().decode())['ready'] is True
        finally:
            srv.stop()

    def test_alerts_endpoint(self):
        m = MetricsCollector(); m.record_alert("TEST", "INFO", "Test alert")
        srv = ObservabilityServer(port=19094, safety=SafetyRails(SweeperConfig(paper_mode=True)), metrics=m)
        srv.start(); time.sleep(0.1)
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:19094/alerts")
            data = json.loads(resp.read().decode())
            assert data['count'] >= 1 and data['alerts'][0]['type'] == 'TEST'
        finally:
            srv.stop()

    def test_status_endpoint(self):
        srv = ObservabilityServer(port=19095, safety=SafetyRails(SweeperConfig(paper_mode=True)), metrics=MetricsCollector())
        srv.start(); time.sleep(0.1)
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:19095/status")
            data = json.loads(resp.read().decode())
            assert 'status' in data and 'metrics' in data
        finally:
            srv.stop()

    def test_health_killed_503(self):
        safety = SafetyRails(SweeperConfig(paper_mode=True))
        safety.state.is_killed = True; safety.state.kill_reason = "Test"
        srv = ObservabilityServer(port=19096, safety=safety, metrics=MetricsCollector())
        srv.start(); time.sleep(0.1)
        try:
            try:
                urllib.request.urlopen("http://127.0.0.1:19096/health")
                assert False, "Should have raised HTTPError"
            except urllib.error.HTTPError as e:
                assert e.code == 503
        finally:
            srv.stop()


class TestLogRotation:
    def test_setup(self, tmp_path):
        h = setup_log_rotation(log_file=str(tmp_path / "test.log"), max_bytes=1024, backup_count=2)
        assert h is not None and h.maxBytes == 1024 and h.backupCount == 2
