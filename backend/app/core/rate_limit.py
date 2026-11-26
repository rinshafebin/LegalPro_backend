"""Redis fixed-window rate limiting.

The client key is `request.client.host`. Uvicorn only rewrites it from
`X-Forwarded-For` when the TCP peer is listed in FORWARDED_ALLOW_IPS, so a
client can't pick its own rate-limit bucket by sending that header. This
module never reads forwarding headers itself.
"""

from collections.abc import Awaitable, Callable

from fastapi import Request

from app.core.errors import RateLimitedError
from app.core.redis import get_redis


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def hit(key: str, limit: int, window_seconds: int) -> None:
    """Count one hit for `key`; raise 429 once `limit` is exceeded in the window."""
    redis = get_redis()
    redis_key = f"rl:{key}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds, nx=True)
        pipe.ttl(redis_key)
        count, _, ttl = await pipe.execute()
    if int(count) > limit:
        retry_after = max(int(ttl), 1)
        raise RateLimitedError(
            "Too many requests, try again later",
            details={"retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


def limit_by_ip(
    scope: str, limit: int, window_seconds: int
) -> Callable[[Request], Awaitable[None]]:
    """Route dependency: `dependencies=[Depends(limit_by_ip("login", 10, 60))]`."""

    async def _dependency(request: Request) -> None:
        await hit(f"{scope}:ip:{client_ip(request)}", limit, window_seconds)

    return _dependency
