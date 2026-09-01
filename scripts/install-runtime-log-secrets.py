#!/usr/bin/env python3
"""Validate an externally issued runtime-log bundle and project least privilege secrets."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("user-service", "order-service", "payment-service")
SOURCE = Path(
    os.environ.get("RUNTIME_LOG_SECRET_SOURCE_DIR", ROOT / "work/runtime-log-pki")
).resolve()
DESTINATION = Path(
    os.environ.get("RUNTIME_LOG_SECRET_DIR", ROOT / "work/runtime-log-secrets")
).resolve()


def run_openssl(*arguments: str) -> str:
    result = subprocess.run(
        ["openssl", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def require_file(name: str) -> Path:
    path = SOURCE / name
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"required regular secret file is missing: {name}")
    return path


def public_key_from_certificate(path: Path) -> str:
    return run_openssl("x509", "-in", str(path), "-pubkey", "-noout")


def public_key_from_private_key(path: Path) -> str:
    return run_openssl("pkey", "-in", str(path), "-pubout")


def validate_pair(certificate: Path, private_key: Path) -> None:
    if public_key_from_certificate(certificate) != public_key_from_private_key(
        private_key
    ):
        raise ValueError(f"certificate and key do not match: {certificate.name}")


def validate_bundle() -> tuple[str, str, dict[str, Path]]:
    metadata = json.loads(require_file("bundle.json").read_text())
    if set(metadata) != {"version", "issuer"}:
        raise ValueError("bundle.json must contain exactly version and issuer")
    version = metadata["version"]
    issuer = metadata["issuer"]
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
        raise ValueError("bundle version must be a safe non-empty identifier")
    if not isinstance(issuer, str) or not issuer.strip():
        raise ValueError("bundle issuer must be a non-empty string")

    files = {
        "ca.pem": require_file("ca.pem"),
        "server-cert.pem": require_file("server-cert.pem"),
        "server-key.pem": require_file("server-key.pem"),
    }
    for service in SERVICES:
        files[f"{service}-cert.pem"] = require_file(f"{service}-cert.pem")
        files[f"{service}-key.pem"] = require_file(f"{service}-key.pem")
    crl = SOURCE / "crl.pem"
    if crl.exists():
        files["crl.pem"] = require_file("crl.pem")

    run_openssl("x509", "-checkend", "300", "-noout", "-in", str(files["server-cert.pem"]))
    run_openssl("x509", "-checkhost", "host.docker.internal", "-noout", "-in", str(files["server-cert.pem"]))
    validate_pair(files["server-cert.pem"], files["server-key.pem"])
    run_openssl("verify", "-purpose", "sslserver", "-CAfile", str(files["ca.pem"]), str(files["server-cert.pem"]))

    for service in SERVICES:
        certificate = files[f"{service}-cert.pem"]
        private_key = files[f"{service}-key.pem"]
        subject = run_openssl("x509", "-noout", "-subject", "-nameopt", "RFC2253", "-in", str(certificate)).strip()
        if f"CN={service}" not in subject.split("subject=", 1)[-1].split(","):
            raise ValueError(f"client certificate CN must be {service}")
        validate_pair(certificate, private_key)
        verify_arguments = ["verify", "-purpose", "sslclient", "-CAfile", str(files["ca.pem"])]
        if "crl.pem" in files:
            verify_arguments.extend(["-crl_check", "-CRLfile", str(files["crl.pem"])])
        run_openssl(*verify_arguments, str(certificate))
    return version, issuer.strip(), files


def copy_file(
    source: Path, destination: Path, mode: int, *, atomic: bool = True
) -> None:
    if destination.exists() and destination.read_bytes() == source.read_bytes():
        os.chmod(destination, mode)
        return
    if not atomic and destination.exists():
        shutil.copyfile(source, destination)
        os.chmod(destination, mode)
        return
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, mode)
    os.replace(temporary, destination)


def project(
    version: str, issuer: str, files: dict[str, Path], phase: str
) -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    lock_path = DESTINATION / ".install.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        version_dir = DESTINATION / "versions" / version
        if version_dir.exists():
            expected_names = set(files) | {"bundle.json"}
            actual_names = {
                path.name for path in version_dir.iterdir() if path.is_file()
            }
            if actual_names != expected_names:
                raise ValueError(
                    f"stored bundle version {version} has different files"
                )
            for name, source in files.items():
                if (version_dir / name).read_bytes() != source.read_bytes():
                    raise ValueError(
                        f"bundle version {version} is immutable: {name} changed"
                    )
        else:
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=version_dir.parent, prefix=".bundle-") as temporary:
                staged = Path(temporary)
                for name, source in files.items():
                    shutil.copyfile(source, staged / name)
                (staged / "bundle.json").write_text(
                    json.dumps({"version": version, "issuer": issuer}, sort_keys=True) + "\n"
                )
                os.chmod(staged / "server-key.pem", 0o600)
                for service in SERVICES:
                    os.chmod(staged / f"{service}-key.pem", 0o600)
                os.replace(staged, version_dir)

        current = DESTINATION / "current"
        gateway = current / "gateway"
        clients = current / "clients"
        gateway.mkdir(parents=True, exist_ok=True)
        clients.mkdir(parents=True, exist_ok=True)
        if phase in {"all", "gateway-trust"}:
            copy_file(files["ca.pem"], gateway / "ca.pem", 0o644)
            crl_destination = gateway / "crl.pem"
            if "crl.pem" in files:
                copy_file(files["crl.pem"], crl_destination, 0o644)
            elif crl_destination.exists():
                crl_destination.unlink()
        if phase in {"all", "clients"}:
            for service in SERVICES:
                client = clients / service
                client.mkdir(parents=True, exist_ok=True)
                # Docker Desktop's daemon-side syslog driver retains the stable
                # path but can briefly lose an atomically replaced inode. The
                # existing logger holds its old credentials while this locked
                # projection changes, before that client is recreated.
                copy_file(
                    files["ca.pem"], client / "ca.pem", 0o644, atomic=False
                )
                copy_file(
                    files[f"{service}-cert.pem"],
                    client / "cert.pem",
                    0o644,
                    atomic=False,
                )
                copy_file(
                    files[f"{service}-key.pem"],
                    client / "key.pem",
                    0o600,
                    atomic=False,
                )
        if phase in {"all", "gateway-identity"}:
            copy_file(
                files["server-cert.pem"], gateway / "server-cert.pem", 0o644
            )
            copy_file(
                files["server-key.pem"], gateway / "server-key.pem", 0o600
            )
        if phase in {"all", "gateway-identity"}:
            fingerprint = hashlib.sha256(files["ca.pem"].read_bytes()).hexdigest()
            (current / "bundle.json").write_text(
                json.dumps(
                    {
                        "version": version,
                        "issuer": issuer,
                        "trust_sha256": fingerprint,
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def main() -> None:
    version, issuer, files = validate_bundle()
    phase = os.environ.get("RUNTIME_LOG_SECRET_INSTALL_PHASE", "all")
    if phase not in {"validate", "all", "gateway-trust", "clients", "gateway-identity"}:
        raise ValueError("invalid RUNTIME_LOG_SECRET_INSTALL_PHASE")
    if phase != "validate":
        project(version, issuer, files, phase)
    print(
        f"Validated runtime-log secret bundle {version} from {issuer}; phase={phase}"
    )


if __name__ == "__main__":
    main()
