import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.chat.models import MAX_MESSAGE_CHARS, ChatRoomKind

MessageBody = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)
]


class AttachmentOut(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


class MessageOut(BaseModel):
    id: uuid.UUID
    seq: int
    room_id: uuid.UUID
    sender_id: uuid.UUID
    client_msg_id: uuid.UUID | None
    body: str | None = Field(description="null when deleted")
    attachment: AttachmentOut | None
    created_at: datetime
    edited: bool = False
    edited_at: datetime | None = None
    deleted: bool = False


class MessageIn(BaseModel):
    """The sender is always the authenticated user; any sender field is ignored."""

    body: MessageBody
    client_msg_id: uuid.UUID | None = Field(
        default=None, description="Client-generated id; resending the same id is a no-op."
    )


class MessageEditIn(BaseModel):
    body: MessageBody


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: int | None = Field(description="Pass as `before` (or `after`) for the next page")


class ReadIn(BaseModel):
    seq: int = Field(ge=0, description="Highest message seq the user has seen")


class ReadStateOut(BaseModel):
    room_id: uuid.UUID
    user_id: uuid.UUID
    last_read_seq: int


class ParticipantOut(BaseModel):
    id: uuid.UUID
    full_name: str
    last_read_seq: int


class RoomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: ChatRoomKind
    case_id: uuid.UUID | None
    case_number: str | None
    booking_id: uuid.UUID | None
    title: str
    last_message_at: datetime | None
    unread_count: int
    last_read_seq: int


class RoomDetailOut(RoomOut):
    participants: list[ParticipantOut]
    writable: bool


class UnreadTotalOut(BaseModel):
    unread: int
