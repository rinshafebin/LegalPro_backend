"""Cross-process fan-out over Redis pub/sub.

Every event is published to one channel per recipient user (`rt:user:<id>`). Each API
process subscribes only to the channels of users connected to it, so an event reaches each
connected socket exactly once no matter which process published it. Recipients are worked
out from the database at publish time, so someone who just lost access gets nothing more.
"""

import json
import uuid
from collections.abc import Iterable
from typing import Any

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

SESSION_REVOKED = "session.revoked"


def user_channel(user_id: uuid.UUID | str) -> str:
    return f"rt:user:{user_id}"


async def publish(user_ids: Iterable[uuid.UUID], event: dict[str, Any]) -> None:
    """Best effort: the data is already committed, and clients catch up over REST."""
    recipients = set(user_ids)
    if not recipients:
        return
    data = json.dumps(event, default=str, separators=(",", ":"))
    try:
        async with get_redis().pipeline(transaction=False) as pipe:
            for user_id in recipients:
                pipe.publish(user_channel(user_id), data)
            await pipe.execute()
    except Exception:
        logger.exception("realtime_publish_failed", event_type=event.get("type"))


async def kick_user(user_id: uuid.UUID) -> None:
    """Close the user's open sockets on every process (logout-all, password change)."""
    await publish([user_id], {"type": SESSION_REVOKED})
