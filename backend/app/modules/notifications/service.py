"""`notify()` is the one way to tell a user something.

It stores the notification (in-app), pushes it to their open sockets, and queues an email
through the sealed email queue unless they opted out of that type. Email bodies are written
by the caller and must never contain privileged content (case titles, notes, chat text):
use the case number and a link.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.email import enqueue_email
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.pagination import PageParams, paginate
from app.modules.notifications.models import Notification, NotificationPreference, NotificationType
from app.modules.notifications.schemas import NotificationOut, PreferenceOut
from app.modules.realtime.pubsub import publish
from app.modules.users.models import User, UserRole

logger = get_logger(__name__)

# Users with admin-only types never see them as options otherwise.
ADMIN_ONLY = frozenset({NotificationType.REFUND_ATTENTION})


def link(path: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/{path.lstrip('/')}"


async def email_enabled(session: AsyncSession, user_id: uuid.UUID, type_: NotificationType) -> bool:
    enabled = await session.scalar(
        select(NotificationPreference.email_enabled).where(
            NotificationPreference.user_id == user_id, NotificationPreference.type == type_.value
        )
    )
    return enabled is not False


async def notify(
    session: AsyncSession,
    user: User,
    type_: NotificationType,
    *,
    title: str,
    data: dict[str, Any] | None = None,
    email: tuple[str, str] | None = None,
) -> Notification | None:
    """Store, push and (maybe) email one notification. Commits; call after your own commit."""
    if not user.is_active:
        return None
    notification = Notification(user_id=user.id, type=type_.value, title=title, data=data or {})
    session.add(notification)
    await session.commit()

    out = NotificationOut.model_validate(notification).model_dump(mode="json")
    await publish([user.id], {"type": "notification", "notification": out})
    if email is not None and await email_enabled(session, user.id, type_):
        await enqueue_email(user.email, *email)
    logger.info("notification_sent", type=type_.value, user_id=str(user.id))
    return notification


async def notify_many(
    session: AsyncSession,
    users: Sequence[User],
    type_: NotificationType,
    *,
    title: str,
    data: dict[str, Any] | None = None,
    email: tuple[str, str] | None = None,
) -> int:
    sent = 0
    for user in users:
        if await notify(session, user, type_, title=title, data=data, email=email):
            sent += 1
    return sent


async def active_admins(session: AsyncSession) -> list[User]:
    stmt = select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
    return list((await session.scalars(stmt)).all())


# ---------------------------------------------------------------- API


async def list_notifications(
    session: AsyncSession, user: User, unread_only: bool, page: PageParams
) -> tuple[list[Notification], int]:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id)
    return await paginate(session, stmt, page)


async def unread_count(session: AsyncSession, user: User) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
    )
    return int(count or 0)


async def mark_read(session: AsyncSession, user: User, notification_id: uuid.UUID) -> Notification:
    notification = await session.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user.id
        )
    )
    if notification is None:
        raise NotFoundError("Notification not found")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await session.commit()
    return notification


async def mark_all_read(session: AsyncSession, user: User) -> int:
    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
        .returning(Notification.id)
    )
    count = len(result.all())
    await session.commit()
    return count


def _types_for(user: User) -> list[NotificationType]:
    return [t for t in NotificationType if user.role is UserRole.ADMIN or t not in ADMIN_ONLY]


async def get_preferences(session: AsyncSession, user: User) -> list[PreferenceOut]:
    rows = dict(
        (
            await session.execute(
                select(NotificationPreference.type, NotificationPreference.email_enabled).where(
                    NotificationPreference.user_id == user.id
                )
            )
        ).all()
    )
    return [PreferenceOut(type=t, email_enabled=rows.get(t.value, True)) for t in _types_for(user)]


async def set_preferences(
    session: AsyncSession, user: User, changes: dict[NotificationType, bool]
) -> list[PreferenceOut]:
    allowed = set(_types_for(user))
    for type_, enabled in changes.items():
        if type_ not in allowed:
            continue
        stmt = pg_insert(NotificationPreference).values(
            user_id=user.id, type=type_.value, email_enabled=enabled
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[NotificationPreference.user_id, NotificationPreference.type],
                set_={"email_enabled": stmt.excluded.email_enabled},
            )
        )
    await session.commit()
    return await get_preferences(session, user)
