"""Password hashing, JWTs, OTP hashing and at-rest encryption helpers."""

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pwdlib import PasswordHash
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings

# ---------------------------------------------------------------- passwords

_password_hash = PasswordHash.recommended()  # argon2id
# Verified when the user does not exist, so login timing doesn't reveal which emails exist.
_DUMMY_HASH = _password_hash.hash(secrets.token_urlsafe(16))


async def hash_password(password: str) -> str:
    # Argon2 is deliberately CPU-heavy: keep it off the event loop.
    return await run_in_threadpool(_password_hash.hash, password)


async def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        await run_in_threadpool(_password_hash.verify, password, _DUMMY_HASH)
        return False
    return await run_in_threadpool(_password_hash.verify, password, password_hash)


# ---------------------------------------------------------------- JWTs


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"
    MFA = "mfa"


@dataclass(frozen=True, slots=True)
class TokenPayload:
    sub: uuid.UUID
    type: TokenType
    jti: str
    iat: datetime
    exp: datetime
    ver: int = 0
    role: str | None = None


class InvalidTokenError(Exception):
    pass


def _encode(claims: dict[str, Any]) -> str:
    s = get_settings()
    return jwt.encode(claims, s.jwt_secret_key.get_secret_value(), algorithm=s.jwt_algorithm)


def create_token(
    user_id: uuid.UUID,
    token_type: TokenType,
    ttl: timedelta,
    *,
    version: int = 0,
    role: str | None = None,
) -> tuple[str, TokenPayload]:
    now = datetime.now(UTC)
    payload = TokenPayload(
        sub=user_id,
        type=token_type,
        jti=uuid.uuid4().hex,
        iat=now,
        exp=now + ttl,
        ver=version,
        role=role,
    )
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type.value,
        "jti": payload.jti,
        "iat": now,
        "exp": payload.exp,
        "ver": version,
    }
    if role is not None:
        claims["role"] = role
    return _encode(claims), payload


def decode_token(token: str, expected_type: TokenType) -> TokenPayload:
    s = get_settings()
    try:
        claims = jwt.decode(
            token,
            s.jwt_secret_key.get_secret_value(),
            algorithms=[s.jwt_algorithm],
            options={"require": ["sub", "type", "jti", "iat", "exp", "ver"]},
        )
        if claims["type"] != expected_type.value:
            raise InvalidTokenError("Wrong token type")
        return TokenPayload(
            sub=uuid.UUID(claims["sub"]),
            type=TokenType(claims["type"]),
            jti=str(claims["jti"]),
            iat=datetime.fromtimestamp(claims["iat"], UTC),
            exp=datetime.fromtimestamp(claims["exp"], UTC),
            ver=int(claims["ver"]),
            role=claims.get("role"),
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("Token has expired") from exc
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise InvalidTokenError("Invalid token") from exc


# ---------------------------------------------------------------- OTP codes


def generate_numeric_code(digits: int = 6) -> str:
    return f"{secrets.randbelow(10**digits):0{digits}d}"


def hash_code(code: str) -> str:
    """Keyed hash for short-lived codes; the plain code is never stored."""
    key = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


def codes_match(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_code(code), code_hash)


# ---------------------------------------------------------------- at-rest encryption


def _fernet() -> Fernet:
    secret = get_settings().secret_key.get_secret_value().encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"field-encryption").derive(
        secret
    )
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_str(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:  # SECRET_KEY rotated without re-encrypting
        raise ValueError("Cannot decrypt value") from exc
