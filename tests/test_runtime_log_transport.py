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
        assert options["syslog-tls-ca-cert"].endswith(
            f"/current/clients/{service_name}/ca.pem"
        )
        assert options["syslog-tls-cert"].endswith(
            f"/current/clients/{service_name}/cert.pem"
        )
        assert options["syslog-tls-key"].endswith(
            f"/current/clients/{service_name}/key.pem"
        )


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
    pki_mounts = [volume for volume in volumes if "/etc/promtail/pki" in volume]

    assert len(pki_mounts) == 1
    assert pki_mounts[0].endswith("/current/gateway:/etc/promtail/pki:ro")
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
    assert "work/runtime-log-secrets/" in gitignore


def test_gateway_supports_zero_listener_gap_tls_reload_and_crl():
    source = GATEWAY_PATH.read_text()
    assert "signal.SIGHUP" in source
    assert "reuse_port=True" in source
    assert "ssl.VERIFY_CRL_CHECK_LEAF" in source
    assert "reload_tls_server" in source
    assert "ACTIVE_CLIENTS" in source
    assert "reauthenticating" in source
    validator = (ROOT / "scripts/validate-runtime-log-mtls.py").read_text()
    assert "RUNTIME_LOG_REVOKED_CLIENT_CERT" in validator
    assert "prove_revoked_client_is_rejected" in validator


def test_runtime_log_secret_installer_projects_only_scoped_material():
    installer = (ROOT / "scripts/install-runtime-log-secrets.py").read_text()
    rotation = (ROOT / "scripts/rotate-runtime-log-certificates.sh").read_text()

    assert 'set(metadata) != {"version", "issuer"}' in installer
    assert '"gateway-trust"' in installer
    assert '"gateway-identity"' in installer
    assert '"-checkhost", "host.docker.internal"' in installer
    assert '"-purpose", "sslclient"' in installer
    assert 'gateway / "server-key.pem"' in installer
    assert 'clients / service' in installer
    assert 'gateway / "ca-key.pem"' not in installer
    assert 'gateway / f"{service}-key.pem"' not in installer
    assert 'atomic=False' in installer
    assert "docker compose kill -s HUP promtail" in rotation
    assert "--force-recreate \"$service\"" in rotation
    assert 'recreate_attempt" -ge 5' in rotation
    assert "opspilot-runtime-log-clients:ro" in rotation
    assert "current_certificate" in rotation
    assert "openssl verify -purpose sslclient" in rotation
    assert "openssl verify -purpose sslserver" in rotation
    assert "RUNTIME_LOG_SECRET_INSTALL_PHASE=gateway-trust" in rotation
    assert "RUNTIME_LOG_SECRET_INSTALL_PHASE=clients" in rotation
    assert "RUNTIME_LOG_SECRET_INSTALL_PHASE=gateway-identity" in rotation
    assert "promtail_id_before" in rotation
    assert 'promtail_id_before" != "$promtail_id_after' in rotation
