"""Recording and listing audit entries.

`record()` only adds the row; the caller commits it together with the change it describes,
so an action and its audit entry succeed or fail together.
"""

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PageParams, paginate
from app.core.rate_limit import client_ip
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.audit.schemas import AuditLogOut
from app.modules.cases.models import Case
from app.modules.users.models import User


@dataclass(frozen=True)
class AuditContext:
    ip: str | None
    request_id: str | None


def audit_context(request: Request) -> AuditContext:
    # Same source as rate limiting: the TCP peer, or X-Forwarded-For only from trusted proxies.
    host = client_ip(request)
    try:
        ip: str | None = str(ipaddress.ip_address(host))
    except ValueError:
        ip = None
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return AuditContext(ip=ip, request_id=str(request_id) if request_id else None)


AuditDep = Annotated[AuditContext, Depends(audit_context)]


def record(
    session: AsyncSession,
    ctx: AuditContext,
    actor_id: uuid.UUID | None,
    action: AuditAction,
    *,
    case_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action.value,
            case_id=case_id,
            entity_type=entity_type,
            entity_id=entity_id,
            ip=ctx.ip,
            request_id=ctx.request_id,
            details=details,
        )
    )


async def list_logs(
    session: AsyncSession,
    page: PageParams,
    *,
    case_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    action: AuditAction | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[AuditLogOut], int]:
    stmt = select(AuditLog)
    if case_id is not None:
        stmt = stmt.where(AuditLog.case_id == case_id)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action.value)
    if since is not None:
        stmt = stmt.where(AuditLog.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.occurred_at < until)
    rows, total = await paginate(session, stmt.order_by(AuditLog.id.desc()), page)

    actor_ids = {r.actor_id for r in rows if r.actor_id}
    case_ids = {r.case_id for r in rows if r.case_id}
    emails = dict(
        (await session.execute(select(User.id, User.email).where(User.id.in_(actor_ids)))).all()
    )
    # Case number only: an identifier, not privileged content.
    numbers = dict(
        (
            await session.execute(select(Case.id, Case.case_number).where(Case.id.in_(case_ids)))
        ).all()
    )
    return [
        AuditLogOut(
            id=r.id,
            occurred_at=r.occurred_at,
            actor_id=r.actor_id,
            actor_email=emails.get(r.actor_id) if r.actor_id else None,
            action=r.action,
            case_id=r.case_id,
            case_number=numbers.get(r.case_id) if r.case_id else None,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            ip=str(r.ip) if r.ip else None,
            request_id=r.request_id,
            details=r.details,
        )
        for r in rows
    ], total
