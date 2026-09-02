import asyncio
import importlib.util
import os
import signal
from pathlib import Path


def load_actuator(tmp_path, monkeypatch, *, allow_stop=True):
    monkeypatch.setenv("RUNTIME_ACTUATOR_TARGET", "redis")
    monkeypatch.setenv("RUNTIME_ACTUATOR_ALLOW_STOP", str(allow_stop).lower())
    monkeypatch.setenv("RUNTIME_ACTUATOR_PROCESS_MATCH", "redis-server")
    monkeypatch.setenv("RUNTIME_ACTUATOR_STATE_DIR", str(tmp_path))
    path = Path("/app/runtime-actuator/app.py")
    if not path.exists():
        path = Path("apps/runtime-actuator/app.py")
    spec = importlib.util.spec_from_file_location(f"runtime_actuator_{id(monkeypatch)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_actuator_stop_is_fixed_target_quarantine_plus_signal(tmp_path, monkeypatch):
    module = load_actuator(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(module, "target_pid", lambda: 42)
    response = asyncio.run(module.stop())

    assert response == {"status": "completed", "result": "stopped redis"}
    assert (tmp_path / "quarantined").read_text(encoding="utf-8") == "redis"
    assert calls == [(42, signal.SIGSTOP)]


def test_actuator_restart_clears_quarantine_and_waits_for_new_target_process(tmp_path, monkeypatch):
    module = load_actuator(tmp_path, monkeypatch)
    calls = []
    identities = iter([(42, "100"), (42, "100"), (43, "200")])
    monkeypatch.setattr(module, "target_identity", lambda: next(identities))
    monkeypatch.setattr(module, "target_state", lambda: "running")
    monkeypatch.setattr(module, "clear_quarantine", lambda: calls.append("clear"))
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    asyncio.run(module.restart_target())

    assert calls == ["clear", (42, signal.SIGCONT), (42, signal.SIGTERM)]


def test_actuator_cpu_stats_are_trimmed_and_monotonic(tmp_path, monkeypatch):
    module = load_actuator(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "target_pid", lambda: 1)
    first = module.cpu_stats()
    second = module.cpu_stats()
    assert set(second) == {
        "target", "cpu_total_usage", "previous_cpu_total_usage", "system_cpu_usage",
        "previous_system_cpu_usage", "online_cpus",
    }
    assert first["target"] == "redis"
    assert second["cpu_total_usage"] >= second["previous_cpu_total_usage"]
    assert second["system_cpu_usage"] >= second["previous_system_cpu_usage"]
