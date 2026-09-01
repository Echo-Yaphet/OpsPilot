import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient


def load_exporter():
    path = Path("/app/container-metrics-exporter/app.py")
    if not path.exists():
        path = Path("apps/container-metrics-exporter/app.py")
    spec = importlib.util.spec_from_file_location("container_metrics_exporter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cpu_usage_ratio_uses_docker_counter_deltas():
    module = load_exporter()
    assert module.cpu_usage_ratio({
        "cpu_total_usage": 300,
        "previous_cpu_total_usage": 100,
        "system_cpu_usage": 2000,
        "previous_system_cpu_usage": 1000,
        "online_cpus": 4,
    }) == 0.8


def test_metrics_expose_per_service_cpu_and_fail_open(monkeypatch):
    module = load_exporter()
    module.TARGETS = ("payment-service", "order-service")

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
