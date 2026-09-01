import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient


def load_exporter(monkeypatch=None, thresholds=None):
    if monkeypatch is not None:
        monkeypatch.setenv(
            "CONTAINER_CPU_THRESHOLDS",
            thresholds or '{"user-service":0.7,"order-service":0.8,"payment-service":0.9}',
        )
    path = Path("/app/container-metrics-exporter/app.py")
    if not path.exists():
        path = Path("apps/container-metrics-exporter/app.py")
    spec = importlib.util.spec_from_file_location("container_metrics_exporter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cpu_usage_ratio_uses_docker_counter_deltas(monkeypatch):
    module = load_exporter(monkeypatch)
    assert module.cpu_usage_ratio({
        "cpu_total_usage": 300,
        "previous_cpu_total_usage": 100,
        "system_cpu_usage": 2000,
        "previous_system_cpu_usage": 1000,
        "online_cpus": 4,
    }) == 0.8


def test_metrics_expose_per_service_cpu_and_fail_open(monkeypatch):
    module = load_exporter(monkeypatch)
    module.TARGETS = ("payment-service", "order-service")
    module.LAST_SUCCESS_TIMESTAMPS["order-service"] = 123.0
    monkeypatch.setattr(module.time, "time", lambda: 456.0)

    async def fake_collect(target):
        if target == "order-service":
            raise RuntimeError("stats unavailable")
        return {
            "cpu_total_usage": 300,
            "previous_cpu_total_usage": 100,
            "system_cpu_usage": 2000,
            "previous_system_cpu_usage": 1000,
            "online_cpus": 4,
        }

    monkeypatch.setattr(module, "collect_target_stats", fake_collect)
    response = TestClient(module.app).get("/metrics")
    assert response.status_code == 200
    assert 'container_cpu_usage_ratio{service="payment-service"} 0.800000' in response.text
    assert 'container_cpu_metrics_up{service="payment-service"} 1' in response.text
    assert 'container_cpu_metrics_up{service="order-service"} 0' in response.text
    assert (
        'container_cpu_metrics_last_success_timestamp_seconds{service="payment-service"} 456.000'
        in response.text
    )
    assert (
        'container_cpu_metrics_last_success_timestamp_seconds{service="order-service"} 123.000'
        in response.text
    )
    assert 'container_cpu_alert_threshold_ratio{service="payment-service"} 0.900000' in response.text
    assert 'container_cpu_alert_threshold_ratio{service="order-service"} 0.800000' in response.text


def test_cpu_thresholds_require_exact_targets_and_valid_values(monkeypatch):
    monkeypatch.setenv("CONTAINER_CPU_THRESHOLDS", '{"payment-service":0.8}')
    try:
        load_exporter()
    except ValueError as exc:
        assert "configure every metrics target exactly once" in str(exc)
    else:
        raise AssertionError("incomplete CPU threshold configuration was accepted")

    monkeypatch.setenv(
        "CONTAINER_CPU_THRESHOLDS",
        '{"user-service":0.8,"order-service":0.8,"payment-service":-1}',
    )
    try:
        load_exporter()
    except ValueError as exc:
        assert "must be in (0, 1024]" in str(exc)
    else:
        raise AssertionError("invalid CPU threshold was accepted")
