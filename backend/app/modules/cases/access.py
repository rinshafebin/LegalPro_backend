"""THE access rule for cases. Every case endpoint goes through `visible_to` / `get_case_for`.

A user can see a case if they are its client, its lead advocate, or a member advocate.
Admins are deliberately not included: case content is privileged.
"""

import uuid
from enum import StrEnum

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.modules.cases.models import Case, CaseMember, CaseStatus
from app.modules.users.models import User


class CaseRole(StrEnum):
    CLIENT = "client"
    LEAD = "lead"
    MEMBER = "member"


ADVOCATE_ROLES = frozenset({CaseRole.LEAD, CaseRole.MEMBER})


def visible_to(user: User) -> ColumnElement[bool]:
    member_of = select(CaseMember.case_id).where(CaseMember.advocate_id == user.id)
    return or_(
        Case.client_id == user.id,
        Case.lead_advocate_id == user.id,
        Case.id.in_(member_of),
    )


async def get_case_for(
    session: AsyncSession, user: User, case_id: uuid.UUID, *, for_update: bool = False
) -> tuple[Case, CaseRole]:
    """Load a case the user may see, with their role. Anyone else gets 404 (not 403)."""
    stmt = select(Case).where(Case.id == case_id, visible_to(user))
    if for_update:
        stmt = stmt.with_for_update()
    case = await session.scalar(stmt)
    if case is None:
        raise NotFoundError("Case not found")
    if case.client_id == user.id:
        return case, CaseRole.CLIENT
    if case.lead_advocate_id == user.id:
        return case, CaseRole.LEAD
    return case, CaseRole.MEMBER


def require_role(role: CaseRole, *allowed: CaseRole, action: str) -> None:
    if role not in allowed:
        raise ForbiddenError(f"You can't {action} on this case")


def require_open(case: Case) -> None:
    if case.status is CaseStatus.CLOSED:
        raise ConflictError("This case is closed; reopen it first", code="case_closed")
