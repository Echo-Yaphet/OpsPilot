import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BUSINESS_SERVICES = ("user-service", "order-service", "payment-service")
GATEWAY_PATH = next(
    path
    for path in (
        ROOT / "apps/promtail/tls_syslog_gateway.py",
        ROOT / "promtail/tls_syslog_gateway.py",
    )
    if path.exists()
)


def test_business_services_use_distinct_mtls_syslog_credentials():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    for service_name in BUSINESS_SERVICES:
        logging = compose["services"][service_name]["logging"]
        options = logging["options"]

        assert logging["driver"] == "syslog"
        assert "tcp+tls://" in options["syslog-address"]
        assert options["syslog-tls-skip-verify"] == "false"
        assert options["syslog-tls-ca-cert"].endswith("/ca.pem")
        assert options["syslog-tls-cert"].endswith(
            f"/{service_name}-cert.pem"
        )
        assert options["syslog-tls-key"].endswith(f"/{service_name}-key.pem")


def test_promtail_requires_ca_verified_client_certificates():
    config = yaml.safe_load(
        (ROOT / "infra/loki/promtail-config.yml").read_text()
    )
    syslog = config["scrape_configs"][0]["syslog"]

    assert syslog["listen_address"] == "127.0.0.1:1515"
    assert syslog["listen_protocol"] == "tcp"
    assert "tls_config" not in syslog

    gateway = _load_gateway_module()
    source = GATEWAY_PATH.read_text()
    assert "ssl.CERT_REQUIRED" in source
    assert "os.setuid(65534)" in source
    assert gateway.ALLOWED_CLIENTS == frozenset(BUSINESS_SERVICES)
    assert gateway.peer_common_name(
        {"subject": ((('commonName', 'payment-service'),),)}
    ) == "payment-service"
    assert gateway.peer_common_name(None) is None


def test_promtail_mount_excludes_ca_and_client_private_keys():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    volumes = compose["services"]["promtail"]["volumes"]
    pki_mounts = [volume for volume in volumes if "/etc/promtail/pki/" in volume]

    assert len(pki_mounts) == 3
    assert any("/server-cert.pem:" in volume for volume in pki_mounts)
    assert any("/server-key.pem:" in volume for volume in pki_mounts)
    assert any("/ca.pem:" in volume for volume in pki_mounts)
    assert all("ca-key.pem" not in volume for volume in pki_mounts)
    assert all("-service-key.pem" not in volume for volume in pki_mounts)


def _load_gateway_module():
    spec = importlib.util.spec_from_file_location("tls_syslog_gateway", GATEWAY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_runtime_log_private_keys_are_git_ignored():
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "work/runtime-log-pki/" in gitignore
