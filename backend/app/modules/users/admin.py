"""Admin user management: list, deactivate, reactivate. Every action is audited."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import SessionDep
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.pagination import Page, PageDep, PageParams, make_page, paginate
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditContext, AuditDep, admin_viewed, record
from app.modules.auth.deps import AdminUser
from app.modules.auth.tokens import revoke_all_sessions
from app.modules.users.models import User, UserRole

logger = get_logger(__name__)


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: UserRole
    is_active: bool
    email_verified: bool
    mfa_enabled: bool
    created_at: datetime
    last_login_at: datetime | None
    deletion_scheduled_for: datetime | None
    deleted_at: datetime | None


class DeactivateIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _out(u: User) -> AdminUserOut:
    return AdminUserOut(
        id=u.id,
        email=u.email,
        role=u.role,
        is_active=u.is_active,
        email_verified=u.email_verified,
        mfa_enabled=u.mfa_enabled,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
        deletion_scheduled_for=u.deletion_scheduled_for,
        deleted_at=u.deleted_at,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_users(
    session: AsyncSession,
    page: PageParams,
    *,
    role: UserRole | None,
    status: Literal["active", "inactive", "all"],
    q: str | None,
) -> tuple[list[AdminUserOut], int]:
    stmt = select(User)
    if role is not None:
        stmt = stmt.where(User.role == role)
    if status != "all":
        stmt = stmt.where(User.is_active.is_(status == "active"))
    if q:
        stmt = stmt.where(User.email.ilike(f"%{_escape_like(q.lower())}%", escape="\\"))
    rows, total = await paginate(session, stmt.order_by(User.created_at.desc(), User.id), page)
    return [_out(u) for u in rows], total


async def _target(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        raise NotFoundError("User not found")
    return user


async def deactivate(
    session: AsyncSession, admin: User, user_id: uuid.UUID, reason: str, audit: AuditContext
) -> User:
    if user_id == admin.id:
        raise ConflictError("You can't deactivate your own account", code="cannot_deactivate_self")
    user = await _target(session, user_id)
    if user.is_active:
        user.is_active = False
        user.token_version += 1  # every outstanding access token stops working
        record(
            session,
            audit,
            admin.id,
            AuditAction.USER_DEACTIVATED,
            entity_type="user",
            entity_id=user.id,
            details={"reason": reason.strip()},
        )
        await session.commit()
        await revoke_all_sessions(user.id)  # refresh tokens + open WebSockets
        logger.info("user_deactivated", user_id=str(user.id), admin_id=str(admin.id))
    return user


async def reactivate(
    session: AsyncSession, admin: User, user_id: uuid.UUID, audit: AuditContext
) -> User:
    user = await _target(session, user_id)
    if user.deleted_at is not None:
        raise ConflictError("This account was deleted", code="account_deleted")
    if user.deletion_scheduled_for is not None:
        raise ConflictError(
            "The user asked for this account to be deleted; only they can cancel it by logging in",
            code="deletion_pending",
        )
    if not user.is_active:
        user.is_active = True
        record(
            session,
            audit,
            admin.id,
            AuditAction.USER_REACTIVATED,
            entity_type="user",
            entity_id=user.id,
        )
        await session.commit()
        logger.info("user_reactivated", user_id=str(user.id), admin_id=str(admin.id))
    return user


router = APIRouter(prefix="/admin/users", tags=["admin"])


@router.get("", response_model=Page[AdminUserOut], dependencies=[Depends(admin_viewed)])
async def admin_list_users(
    _: AdminUser,
    session: SessionDep,
    page: PageDep,
    role: UserRole | None = None,
    status: Literal["active", "inactive", "all"] = "all",
    q: Annotated[
        str | None, Query(min_length=2, max_length=100, description="Email contains")
    ] = None,
) -> Page[AdminUserOut]:
    rows, total = await list_users(session, page, role=role, status=status, q=q)
    return make_page(rows, total, page)


@router.post(
    "/{user_id}/deactivate",
    response_model=AdminUserOut,
    description="Disables login, revokes every session and closes open sockets. Audited.",
)
async def admin_deactivate(
    user_id: uuid.UUID, data: DeactivateIn, admin: AdminUser, session: SessionDep, audit: AuditDep
) -> AdminUserOut:
    return _out(await deactivate(session, admin, user_id, data.reason, audit))


@router.post("/{user_id}/reactivate", response_model=AdminUserOut, description="Audited.")
async def admin_reactivate(
    user_id: uuid.UUID, admin: AdminUser, session: SessionDep, audit: AuditDep
) -> AdminUserOut:
    return _out(await reactivate(session, admin, user_id, audit))
