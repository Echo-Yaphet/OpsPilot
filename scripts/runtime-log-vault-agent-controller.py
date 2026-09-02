#!/usr/bin/env python3
"""Apply an atomic Vault Agent KV v2 render through the runtime-log bundle seam."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("user-service", "order-service", "payment-service")
REQUIRED_FILES = {
    "bundle.json",
    "ca.pem",
    "server-cert.pem",
    "server-key.pem",
    *(f"{service}-cert.pem" for service in SERVICES),
    *(f"{service}-key.pem" for service in SERVICES),
}
OPTIONAL_FILES = {"crl.pem"}
MAX_RENDER_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class VaultRender:
    vault_kv_version: int
    bundle_version: str
    issuer: str
    files: dict[str, bytes]
    digest: str


def load_render(path: Path) -> VaultRender:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError("Vault Agent render must be a regular non-symlink file")
    if metadata.st_size <= 0 or metadata.st_size > MAX_RENDER_BYTES:
        raise ValueError("Vault Agent render has an invalid size")
    envelope = json.loads(path.read_text())
    if not isinstance(envelope, dict) or set(envelope) != {"vault_kv_version", "data"}:
        raise ValueError("Vault Agent render must contain exactly vault_kv_version and data")
    vault_version = envelope["vault_kv_version"]
    if not isinstance(vault_version, int) or isinstance(vault_version, bool) or vault_version < 1:
        raise ValueError("vault_kv_version must be a positive integer")
    data = envelope["data"]
    if not isinstance(data, dict):
        raise ValueError("Vault Agent data must be an object")
    names = set(data)
    if not REQUIRED_FILES.issubset(names) or names - REQUIRED_FILES - OPTIONAL_FILES:
        raise ValueError("Vault Agent data has missing or unexpected runtime-log files")
    if not all(isinstance(value, str) and value for value in data.values()):
        raise ValueError("Vault Agent runtime-log values must be non-empty strings")

    bundle = json.loads(data["bundle.json"])
    if not isinstance(bundle, dict) or set(bundle) != {"version", "issuer"}:
        raise ValueError("rendered bundle.json must contain exactly version and issuer")
    bundle_version = bundle["version"]
    issuer = bundle["issuer"]
    if not isinstance(bundle_version, str) or not re.fullmatch(
        r"[A-Za-z0-9._-]{1,80}", bundle_version
    ):
        raise ValueError("rendered bundle version is invalid")
    if not isinstance(issuer, str) or not issuer.strip():
        raise ValueError("rendered bundle issuer is invalid")

    files = {name: value.encode() for name, value in data.items()}
    digest_input = b"".join(
        name.encode() + b"\0" + files[name] + b"\0" for name in sorted(files)
    )
    return VaultRender(
        vault_kv_version=vault_version,
        bundle_version=bundle_version,
        issuer=issuer.strip(),
        files=files,
        digest=hashlib.sha256(digest_input).hexdigest(),
    )


def load_state(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    required = {"vault_kv_version", "bundle_version", "digest", "accepted_at"}
    if not isinstance(state, dict) or set(state) != required:
        raise ValueError("Vault Agent controller state is malformed")
    return state


def ensure_not_rollback(render: VaultRender, state: dict[str, object] | None) -> bool:
    if state is None:
        return True
    accepted_version = state["vault_kv_version"]
    if not isinstance(accepted_version, int):
        raise ValueError("Vault Agent controller state version is malformed")
    if render.vault_kv_version < accepted_version:
        raise ValueError(
            f"Vault KV rollback rejected: {render.vault_kv_version} < {accepted_version}"
        )
    if render.vault_kv_version == accepted_version:
        if render.digest != state["digest"] or render.bundle_version != state["bundle_version"]:
            raise ValueError("accepted Vault KV version changed content")
        return False
    return True


def materialize_snapshot(render: VaultRender, snapshot_root: Path) -> Path:
    snapshot_root.mkdir(parents=True, exist_ok=True)
    destination = snapshot_root / f"kv-{render.vault_kv_version}-{render.bundle_version}"
    if destination.exists():
        actual = {
            path.name: path.read_bytes()
            for path in destination.iterdir()
            if path.is_file() and not path.is_symlink()
        }
        if actual != render.files:
            raise ValueError("immutable Vault KV snapshot changed content")
        return destination
    with tempfile.TemporaryDirectory(dir=snapshot_root, prefix=".vault-render-") as temporary:
        staged = Path(temporary)
        for name, content in render.files.items():
            target = staged / name
            target.write_bytes(content)
            os.chmod(target, 0o600 if name.endswith("-key.pem") else 0o644)
        os.replace(staged, destination)
    return destination


def run_checked(
    command: list[str], source: Path, extra_environment: dict[str, str] | None = None
) -> None:
    environment = os.environ.copy()
    environment["RUNTIME_LOG_SECRET_SOURCE_DIR"] = str(source)
    environment.update(extra_environment or {})
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def write_state(path: Path, render: VaultRender) -> None:
    state = {
        "vault_kv_version": render.vault_kv_version,
        "bundle_version": render.bundle_version,
        "digest": render.digest,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".state-", delete=False
    ) as temporary:
        json.dump(state, temporary, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


def apply(rendered_path: Path, work_dir: Path, validate_only: bool = False) -> str:
    work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = work_dir / ".controller.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        render = load_render(rendered_path)
        state_path = work_dir / "accepted.json"
        should_apply = ensure_not_rollback(render, load_state(state_path))
        if not should_apply:
            return f"Vault KV version {render.vault_kv_version} already accepted"
        snapshot = materialize_snapshot(render, work_dir / "snapshots")
        if validate_only:
            run_checked(
                [str(ROOT / "scripts/install-runtime-log-secrets.py")],
                snapshot,
                {"RUNTIME_LOG_SECRET_INSTALL_PHASE": "validate"},
            )
            return (
                f"Validated Vault KV version {render.vault_kv_version} "
                f"as bundle {render.bundle_version}"
            )
        secret_dir = Path(
            os.environ.get(
                "RUNTIME_LOG_SECRET_DIR", ROOT / "work/runtime-log-secrets"
            )
        )
        if (secret_dir / "current/bundle.json").exists():
            run_checked(
                [str(ROOT / "scripts/rotate-runtime-log-certificates.sh")], snapshot
            )
        else:
            run_checked(
                [str(ROOT / "scripts/install-runtime-log-secrets.py")], snapshot
            )
        write_state(state_path, render)
        return (
            f"Accepted Vault KV version {render.vault_kv_version} "
            f"as bundle {render.bundle_version}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rendered",
        type=Path,
        default=ROOT / "work/runtime-log-vault-agent/rendered-bundle.json",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=ROOT / "work/runtime-log-vault-agent/controller",
    )
    parser.add_argument("--validate-only", action="store_true")
    arguments = parser.parse_args()
    print(
        apply(
            arguments.rendered.resolve(),
            arguments.work_dir.resolve(),
            arguments.validate_only,
        )
    )


if __name__ == "__main__":
    main()
