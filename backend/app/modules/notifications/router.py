import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import SessionDep
from app.core.pagination import Page, PageDep, make_page
from app.modules.auth.deps import CurrentUser
from app.modules.notifications import service
from app.modules.notifications.schemas import (
    NotificationOut,
    PreferenceOut,
    PreferencesIn,
    UnreadCountOut,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationOut])
async def list_notifications(
    user: CurrentUser,
    session: SessionDep,
    page: PageDep,
    unread: Annotated[bool, Query(description="Only unread notifications")] = False,
) -> Page[NotificationOut]:
    rows, total = await service.list_notifications(session, user, unread, page)
    return make_page([NotificationOut.model_validate(n) for n in rows], total, page)


@router.get("/unread-count", response_model=UnreadCountOut)
async def unread_count(user: CurrentUser, session: SessionDep) -> UnreadCountOut:
    return UnreadCountOut(unread=await service.unread_count(session, user))


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> NotificationOut:
    return NotificationOut.model_validate(await service.mark_read(session, user, notification_id))


@router.post("/read-all", response_model=UnreadCountOut, summary="Mark all as read")
async def mark_all_read(user: CurrentUser, session: SessionDep) -> UnreadCountOut:
    await service.mark_all_read(session, user)
    return UnreadCountOut(unread=0)


@router.get("/preferences", response_model=list[PreferenceOut])
async def get_preferences(user: CurrentUser, session: SessionDep) -> list[PreferenceOut]:
    return await service.get_preferences(session, user)


@router.put(
    "/preferences",
    response_model=list[PreferenceOut],
    description="Turn email on/off per notification type. In-app notifications always stay on.",
)
async def set_preferences(
    data: PreferencesIn, user: CurrentUser, session: SessionDep
) -> list[PreferenceOut]:
    return await service.set_preferences(session, user, data.email)
