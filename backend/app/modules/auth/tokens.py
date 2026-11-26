"""Refresh-token sessions, MFA challenge tokens and TOTP replay protection, all in Redis.

Refresh tokens rotate on every use. Each refresh token's `jti` is a key in
Redis; using a token deletes its key. If a token arrives whose key is already
gone (it was used or revoked), that is treated as theft and every session of
the user is revoked.
"""

import hmac
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import cast

import pyotp

from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.security import TokenPayload, TokenType, create_token
from app.modules.auth.schemas import TokenPair
from app.modules.realtime.pubsub import kick_user
from app.modules.users.models import User

_REFRESH = "auth:refresh:{jti}"
_SESSIONS = "auth:sessions:{user_id}"
_MFA_ATTEMPTS = "auth:mfa_attempts:{jti}"
_MFA_USED = "auth:mfa_used:{jti}"
_MFA_SETUP = "auth:mfa_setup:{user_id}"
_TOTP_USED = "auth:totp_used:{user_id}:{timecode}"

MFA_MAX_ATTEMPTS = 5
MFA_SETUP_TTL_SECONDS = 600


def _seconds_until(moment: datetime) -> int:
    return max(int((moment - datetime.now(UTC)).total_seconds()), 1)


# ---------------------------------------------------------------- access/refresh


async def issue_token_pair(user: User) -> TokenPair:
    s = get_settings()
    access, _ = create_token(
        user.id,
        TokenType.ACCESS,
        timedelta(minutes=s.access_token_ttl_minutes),
        version=user.token_version,
        role=user.role.value,
    )
    refresh, payload = create_token(
        user.id,
        TokenType.REFRESH,
        timedelta(days=s.refresh_token_ttl_days),
        version=user.token_version,
    )
    redis = get_redis()
    sessions_key = _SESSIONS.format(user_id=user.id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(_REFRESH.format(jti=payload.jti), str(user.id), ex=_seconds_until(payload.exp))
        pipe.sadd(sessions_key, payload.jti)
        pipe.expire(sessions_key, s.refresh_token_ttl_days * 86400)
        await pipe.execute()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=s.access_token_ttl_minutes * 60,
    )


async def consume_refresh(payload: TokenPayload) -> bool:
    """Atomically invalidate a refresh token. False means it was already used or revoked."""
    redis = get_redis()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(_REFRESH.format(jti=payload.jti))
        pipe.srem(_SESSIONS.format(user_id=payload.sub), payload.jti)
        deleted, _ = await pipe.execute()
    return int(deleted) == 1


async def revoke_all_sessions(user_id: uuid.UUID) -> None:
    redis = get_redis()
    sessions_key = _SESSIONS.format(user_id=user_id)
    jtis = await cast(Awaitable[set[str]], redis.smembers(sessions_key))
    keys = [_REFRESH.format(jti=jti) for jti in jtis]
    await redis.delete(sessions_key, *keys)
    await kick_user(user_id)  # close open WebSockets on every API process


# ---------------------------------------------------------------- MFA challenge


def issue_mfa_token(user: User) -> str:
    token, _ = create_token(
        user.id,
        TokenType.MFA,
        timedelta(seconds=get_settings().mfa_token_ttl_seconds),
        version=user.token_version,
    )
    return token


async def register_mfa_attempt(payload: TokenPayload) -> bool:
    """Count an attempt against this MFA token. False once the token is burned."""
    redis = get_redis()
    if await redis.exists(_MFA_USED.format(jti=payload.jti)):
        return False
    key = _MFA_ATTEMPTS.format(jti=payload.jti)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, _seconds_until(payload.exp), nx=True)
        attempts, _ = await pipe.execute()
    return int(attempts) <= MFA_MAX_ATTEMPTS


async def mark_mfa_token_used(payload: TokenPayload) -> bool:
    """Single use: returns False if this MFA token was already exchanged."""
    redis = get_redis()
    stored = await redis.set(
        _MFA_USED.format(jti=payload.jti), "1", nx=True, ex=_seconds_until(payload.exp)
    )
    return bool(stored)


# ---------------------------------------------------------------- TOTP


async def store_pending_mfa_secret(user_id: uuid.UUID, encrypted_secret: str) -> None:
    await get_redis().set(
        _MFA_SETUP.format(user_id=user_id), encrypted_secret, ex=MFA_SETUP_TTL_SECONDS
    )


async def pop_pending_mfa_secret(user_id: uuid.UUID) -> str | None:
    value = await get_redis().getdel(_MFA_SETUP.format(user_id=user_id))
    return str(value) if value is not None else None


async def get_pending_mfa_secret(user_id: uuid.UUID) -> str | None:
    value = await get_redis().get(_MFA_SETUP.format(user_id=user_id))
    return str(value) if value is not None else None


def _matching_timecode(secret: str, code: str) -> int | None:
    totp = pyotp.TOTP(secret)
    now = datetime.now(UTC)
    for step in (-1, 0, 1):  # tolerate ±30 s clock drift
        moment = now + timedelta(seconds=step * totp.interval)
        if hmac.compare_digest(totp.at(moment), code):
            return int(totp.timecode(moment))
    return None


async def verify_totp(user_id: uuid.UUID, secret: str, code: str) -> bool:
    """Check a TOTP code and refuse to accept the same code twice."""
    timecode = _matching_timecode(secret, code)
    if timecode is None:
        return False
    first_use = await get_redis().set(
        _TOTP_USED.format(user_id=user_id, timecode=timecode), "1", nx=True, ex=120
    )
    return bool(first_use)
