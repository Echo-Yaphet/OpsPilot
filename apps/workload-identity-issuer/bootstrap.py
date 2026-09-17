import base64
import json
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
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
    ("/identity/policy-controller-private", "/identity/policy-controller-public"),
    ("/identity/access-private", "/identity/access-public"),
):
    ensure_key_pair(private_dir, public_dir)


def encode(value: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


def write_alertmanager_token() -> None:
    now = int(time.time())
    header = encode({"alg": "RS256", "kid": "opspilot-api-access-v1", "typ": "JWT"})
    claims = encode({
        "iss": "opspilot-local-access-issuer", "aud": "opspilot-control-api",
        "sub": "local-alertmanager", "roles": ["alertmanager"], "iat": now,
        "exp": now + 30 * 24 * 60 * 60, "jti": str(uuid.uuid4()),
    })
    signed = f"{header}.{claims}"
    key = serialization.load_pem_private_key(
        Path("/identity/access-private/private.pem").read_bytes(), password=None
    )
    signature = key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    token = f"{signed}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    target = Path("/identity/access-alertmanager/token")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(token)
    target.chmod(0o644)


write_alertmanager_token()
