from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def ensure_key_pair(private_directory: str, public_directory: str) -> None:
    private_root, public_root = Path(private_directory), Path(public_directory)
    private_root.mkdir(parents=True, exist_ok=True)
    public_root.mkdir(parents=True, exist_ok=True)
    private_path, public_path = private_root / "private.pem", public_root / "public.pem"
    if private_path.exists() and public_path.exists():
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    public_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    private_path.chmod(0o600)
    public_path.chmod(0o644)


for private_dir, public_dir in (
    ("/identity/issuer-private", "/identity/issuer-public"),
    ("/identity/control-private", "/identity/control-public"),
    ("/identity/gateway-private", "/identity/gateway-public"),
    ("/identity/metrics-private", "/identity/metrics-public"),
):
    ensure_key_pair(private_dir, public_dir)
