import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass


PBKDF2_ITERATIONS = 210_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def create_access_token(
    public_id: str,
    role: str,
    secret: str,
    ttl_seconds: int,
) -> str:
    payload = {
        "sub": public_id,
        "role": role,
        "exp": int(time.time()) + ttl_seconds,
    }
    body = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64encode(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{signature}"


@dataclass(frozen=True)
class TokenPayload:
    public_id: str
    role: str


def decode_access_token(token: str, secret: str) -> TokenPayload | None:
    try:
        body, provided_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(
                secret.encode("utf-8"),
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(provided_signature, expected_signature):
            return None
        payload = json.loads(_b64decode(body))
        if int(payload["exp"]) < int(time.time()):
            return None
        role = str(payload["role"])
        if role not in {"student", "admin"}:
            return None
        return TokenPayload(public_id=str(payload["sub"]), role=role)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None

