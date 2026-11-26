import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import SessionDep
from app.core.pagination import Page, PageDep, make_page
from app.modules.audit import service
from app.modules.audit.models import AuditAction
from app.modules.audit.schemas import AuditLogOut

router = APIRouter(
    prefix="/admin/audit-logs",
    tags=["admin"],
    dependencies=[Depends(service.admin_viewed)],  # includes the admin + MFA check
)


@router.get("", response_model=Page[AuditLogOut], description="Newest first. Admins only.")
async def list_audit_logs(
    session: SessionDep,
    page: PageDep,
    case_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    since: Annotated[datetime | None, Query(description="Inclusive, ISO 8601")] = None,
    until: Annotated[datetime | None, Query(description="Exclusive, ISO 8601")] = None,
) -> Page[AuditLogOut]:
    rows, total = await service.list_logs(
        session, page, case_id=case_id, actor_id=actor_id, action=action, since=since, until=until
    )
    return make_page(rows, total, page)
