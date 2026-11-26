import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.modules.notifications.models import NotificationType


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    data: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread: int


class PreferenceOut(BaseModel):
    type: NotificationType
    email_enabled: bool


class PreferencesIn(BaseModel):
    """Only the types listed are changed."""

    email: dict[NotificationType, bool]
