"""Password hashing, JWTs, OTP hashing and at-rest encryption helpers."""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
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
    # True only for sessions that passed a TOTP check at login (carried through refresh).
    mfa: bool = False


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
    mfa: bool = False,
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
        mfa=mfa,
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
    if mfa:
        claims["mfa"] = True
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
            mfa=claims.get("mfa") is True,
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


@lru_cache(maxsize=4)
def _build_multi_fernet(keys: tuple[str, ...]) -> MultiFernet:
    return MultiFernet([Fernet(key) for key in keys])


def _multi_fernet() -> MultiFernet:
    keys = tuple(k.get_secret_value() for k in get_settings().field_encryption_key)
    return _build_multi_fernet(keys)


def encrypt_str(value: str) -> str:
    """Encrypt with the primary (first) FIELD_ENCRYPTION_KEY."""
    return _multi_fernet().encrypt(value.encode()).decode()


def decrypt_str(value: str) -> str:
    """Decrypt with any configured FIELD_ENCRYPTION_KEY."""
    try:
        return _multi_fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt value with any configured FIELD_ENCRYPTION_KEY") from exc


def rotate_encrypted_str(value: str) -> str:
    """Re-encrypt a value under the primary key (it may currently use an older key)."""
    try:
        return _multi_fernet().rotate(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt value with any configured FIELD_ENCRYPTION_KEY") from exc


# ---------------------------------------------------------------- email verification


def _email_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_settings().secret_key.get_secret_value(), salt="email-verification"
    )


def create_email_verification_token(user_id: uuid.UUID, email: str) -> str:
    """Signed token bound to the user AND the address, so it dies if the email changes."""
    return _email_serializer().dumps({"uid": str(user_id), "email": email})


def read_email_verification_token(token: str) -> tuple[uuid.UUID, str]:
    max_age = get_settings().email_verification_ttl_hours * 3600
    try:
        data = _email_serializer().loads(token, max_age=max_age)
        return uuid.UUID(data["uid"]), str(data["email"])
    except SignatureExpired as exc:
        raise InvalidTokenError("Verification link has expired") from exc
    except (BadSignature, KeyError, TypeError, ValueError) as exc:
        raise InvalidTokenError("Invalid verification link") from exc
