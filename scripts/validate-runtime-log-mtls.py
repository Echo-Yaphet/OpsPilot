#!/usr/bin/env python3
import json
import os
import socket
import ssl
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PKI_DIR = Path(
    os.getenv("RUNTIME_LOG_PKI_DIR", PROJECT_ROOT / "work/runtime-log-pki")
)
ADDRESS = ("127.0.0.1", 1514)
SERVER_NAME = "host.docker.internal"


def tls_context(client_name: str | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=PKI_DIR / "ca.pem")
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if client_name:
        context.load_cert_chain(
            certfile=PKI_DIR / f"{client_name}-cert.pem",
            keyfile=PKI_DIR / f"{client_name}-key.pem",
        )
    return context


def prove_missing_client_certificate_is_rejected() -> None:
    try:
        with socket.create_connection(ADDRESS, timeout=3) as connection:
            with tls_context().wrap_socket(
                connection, server_hostname=SERVER_NAME
            ) as secured:
                secured.sendall(b"unauthenticated runtime log probe\n")
                if secured.recv(1) == b"":
                    return
    except (ssl.SSLError, ConnectionError):
        return
    raise RuntimeError("runtime log receiver accepted a client without a certificate")


def prove_wrong_server_name_is_rejected() -> None:
    try:
        with socket.create_connection(ADDRESS, timeout=3) as connection:
            tls_context("payment-service").wrap_socket(
                connection, server_hostname="wrong.runtime-log.invalid"
            )
    except ssl.SSLCertVerificationError:
        return
    raise RuntimeError("runtime log client accepted a certificate for the wrong server")


def send_authenticated_probe() -> str:
    marker = f"opspilot-mtls-acceptance-{time.time_ns()}"
    timestamp = datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    payload = (
        f"<14>1 {timestamp} docker-desktop opspilot|payment-service - - - "
        f"{marker}"
    ).encode()
    message = str(len(payload)).encode() + b" " + payload
    with socket.create_connection(ADDRESS, timeout=3) as connection:
        with tls_context("payment-service").wrap_socket(
            connection, server_hostname=SERVER_NAME
        ) as secured:
            secured.sendall(message)
    return marker


def wait_for_loki(marker: str) -> None:
    parameters = urllib.parse.urlencode(
        {
            "query": f'{{compose_service="payment-service"}} |= "{marker}"',
            "limit": "1",
            "direction": "backward",
            "since": "1m",
        }
    )
    url = f"http://127.0.0.1:3100/loki/api/v1/query_range?{parameters}"
    for _ in range(10):
        with urllib.request.urlopen(url, timeout=3) as response:
            result = json.load(response)["data"]["result"]
        if result:
            return
        time.sleep(1)
    raise RuntimeError("authenticated runtime log probe did not reach Loki")


def main() -> None:
    prove_missing_client_certificate_is_rejected()
    prove_wrong_server_name_is_rejected()
    marker = send_authenticated_probe()
    wait_for_loki(marker)
    print("runtime log mTLS authentication and Loki delivery verified")


if __name__ == "__main__":
    main()
