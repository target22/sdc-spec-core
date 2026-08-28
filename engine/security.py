"""
engine/security.py — the trust boundary: secret loading, admin
authentication, and encryption-at-rest for anything written to the
DLQ. Everything here is either a pure function (encrypt/decrypt) or a
FastAPI auth dependency; nothing here knows about contracts, DAGs, or
any customer's business logic.
"""
import base64
import hashlib
import os
import secrets
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv
from fastapi import Header, HTTPException

load_dotenv()


ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
if not ADMIN_API_KEY:
    # An unauthenticated Judicial Layer is not a Judicial Layer. Fail at
    # boot rather than serving an open admin boundary.
    raise RuntimeError(
        "ADMIN_API_KEY is not set. Define it in .env (see .env template) "
        "before starting the engine."
    )

_DLQ_ENCRYPTION_KEY_B64 = os.getenv("DLQ_ENCRYPTION_KEY")
if not _DLQ_ENCRYPTION_KEY_B64:
    # Same failure philosophy as ADMIN_API_KEY above: an encryption-at-rest
    # feature with no key is not a feature, it's a silent no-op that would
    # make every future engineer believe DLQ payloads are protected when
    # they are not. Fail at boot instead.
    raise RuntimeError(
        "DLQ_ENCRYPTION_KEY is not set. Generate one with, e.g.:\n"
        "  python3 -c \"import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())\"\n"
        "and define it in .env before starting the engine."
    )
try:
    DLQ_ENCRYPTION_KEY: bytes = base64.b64decode(_DLQ_ENCRYPTION_KEY_B64)
    if len(DLQ_ENCRYPTION_KEY) != 32:
        raise ValueError(f"decoded key is {len(DLQ_ENCRYPTION_KEY)} bytes, need exactly 32 (AES-256)")
except Exception as e:
    raise RuntimeError(f"DLQ_ENCRYPTION_KEY is not valid base64-encoded 32-byte key material: {e}")

# --- Value Proxy: reversible AES-256-GCM encryption for DLQ payloads ---
_ENC_MAX_DEPTH = 12  # defense-in-depth against pathological nesting
_AESGCM = AESGCM(DLQ_ENCRYPTION_KEY)


def _fingerprint(raw: str) -> str:
    """8-hex-char SHA-256 prefix -- correlation signal only, never used for
    reversal (impossible by construction)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _encrypt_str(raw: str) -> Dict[str, str]:
    nonce = secrets.token_bytes(12)  # 96-bit nonce, unique per encryption call
    ciphertext = _AESGCM.encrypt(nonce, raw.encode("utf-8"), associated_data=None)
    return {
        "__enc__": "aesgcm",
        "n": base64.b64encode(nonce).decode("ascii"),
        "c": base64.b64encode(ciphertext).decode("ascii"),
    }


def _decrypt_str(enc: Dict[str, Any]) -> str:
    nonce = base64.b64decode(enc["n"])
    ciphertext = base64.b64decode(enc["c"])
    plaintext = _AESGCM.decrypt(nonce, ciphertext, associated_data=None)
    return plaintext.decode("utf-8")


def _encrypt_value(value: Any, depth: int = 0) -> Any:
    if depth >= _ENC_MAX_DEPTH:
        return "<max-depth-exceeded>"
    if value is None or isinstance(value, bool):
        return value  # null/bool carry no PII; keep as-is for readability
    if isinstance(value, (int, float)):
        raw = str(value)
        return {"type": type(value).__name__, "fp": _fingerprint(raw), **_encrypt_str(raw)}
    if isinstance(value, str):
        if not value:
            return ""
        return {"type": "str", "len": len(value), "fp": _fingerprint(value), **_encrypt_str(value)}
    if isinstance(value, list):
        return [_encrypt_value(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: _encrypt_value(v, depth + 1) for k, v in value.items()}
    return f"<{type(value).__name__}>"


def _decrypt_value(value: Any) -> Any:
    """Inverse of _encrypt_value. Used only by the admin decrypt endpoint.
    Leaves non-encrypted-shaped values (null, bool, plain "<...>" markers,
    already-empty strings) untouched; recurses through list/dict structure
    exactly as encryption did."""
    if isinstance(value, dict):
        if "__enc__" in value and "n" in value and "c" in value:
            try:
                plaintext = _decrypt_str(value)
            except Exception as e:  # pragma: no cover -- corrupt/foreign entry
                return f"<decrypt-failed: {e}>"
            vtype = value.get("type")
            if vtype == "int":
                try:
                    return int(plaintext)
                except ValueError:
                    return plaintext
            if vtype == "float":
                try:
                    return float(plaintext)
                except ValueError:
                    return plaintext
            return plaintext
        return {k: _decrypt_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decrypt_value(v) for v in value]
    return value


def sanitize_payload(payload: Optional[dict]) -> Optional[dict]:
    """Public entrypoint: structure-preserving, encryption-at-rest proxy
    for anything about to be written to the DLQ. Name kept stable across
    the hashing->encryption change -- every existing call site (dlq_log
    below, and any future one) needs no change."""
    if payload is None:
        return None
    return _encrypt_value(payload)


def decrypt_payload(payload: Optional[dict]) -> Optional[dict]:
    """Public entrypoint for the admin decrypt endpoint -- exact inverse
    of sanitize_payload()."""
    if payload is None:
        return None
    return _decrypt_value(payload)


# --- Admin auth dependencies (FastAPI Depends targets) -----------------
async def verify_admin_key(x_admin_api_key: str = Header(..., alias="X-Admin-API-Key")) -> None:
    if not secrets.compare_digest(x_admin_api_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-API-Key")


async def verify_dlq_admin_key(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    """Distinct header name per Req 3 ('X-Admin-Key'), checked against the
    SAME ADMIN_API_KEY secret already required at boot -- one credential
    to provision and rotate, not two. Decryption capability is strictly
    more sensitive than contract upload, so if these two scopes need to
    diverge later, split the env var then; do not do it preemptively."""
    if not secrets.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key")
