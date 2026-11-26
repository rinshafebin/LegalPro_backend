"""Short-lived, single-use WebSocket tickets.

Browsers can't set an Authorization header on a WebSocket, and a JWT in the URL ends up in
proxy and server logs. Instead the client trades its access token for a random ticket over
an authenticated POST and connects with `?ticket=...`. The ticket lives a few seconds, is
stored only as a hash, and is deleted the first time it's used.
"""

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.redis import get_redis
from app.modules.users.models import User

_KEY = "ws:ticket:{digest}"


@dataclass(frozen=True)
class TicketClaim:
    user_id: uuid.UUID
    token_version: int


def _key(ticket: str) -> str:
    return _KEY.format(digest=hashlib.sha256(ticket.encode()).hexdigest())


async def issue_ticket(user: User) -> tuple[str, int]:
    ttl = get_settings().ws_ticket_ttl_seconds
    ticket = secrets.token_urlsafe(32)
    claim = json.dumps({"uid": str(user.id), "ver": user.token_version})
    await get_redis().set(_key(ticket), claim, ex=ttl)
    return ticket, ttl


async def consume_ticket(ticket: str) -> TicketClaim | None:
    if not ticket or len(ticket) > 128:
        return None
    raw = await get_redis().getdel(_key(ticket))  # atomic: a second use finds nothing
    if raw is None:
        return None
    data = json.loads(raw)
    return TicketClaim(user_id=uuid.UUID(data["uid"]), token_version=int(data["ver"]))
