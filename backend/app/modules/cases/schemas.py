import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.modules.cases.access import CaseRole
from app.modules.cases.models import (
    CaseMemberRole,
    CasePriority,
    CaseResult,
    CaseStatus,
    HearingStatus,
)


class PersonOut(BaseModel):
    id: uuid.UUID
    full_name: str


class CaseMemberOut(PersonOut):
    role: CaseMemberRole
    added_at: datetime


class CaseCreateIn(BaseModel):
    booking_id: uuid.UUID = Field(description="A confirmed or completed consultation of yours")
    title: str = Field(min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    priority: CasePriority = CasePriority.MEDIUM
    court_name: str | None = Field(default=None, max_length=200)


class CaseUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    status: CaseStatus | None = None
    priority: CasePriority | None = None
    result: CaseResult | None = None
    court_name: str | None = Field(default=None, max_length=200)


class CaseSummaryOut(BaseModel):
    id: uuid.UUID
    case_number: str
    title: str
    status: CaseStatus
    priority: CasePriority
    my_role: CaseRole
    updated_at: datetime


class CaseOut(CaseSummaryOut):
    description: str | None
    result: CaseResult
    court_name: str | None
    client: PersonOut
    lead_advocate: PersonOut
    members: list[CaseMemberOut]
    booking_id: uuid.UUID | None
    created_at: datetime
    closed_at: datetime | None


class CaseMemberAddIn(BaseModel):
    advocate_id: uuid.UUID
    role: CaseMemberRole = CaseMemberRole.ASSOCIATE


# ---------------------------------------------------------------- notes


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)
    is_private: bool = Field(default=False, description="Hidden from the client")


class NoteUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=20_000)


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID | None
    body: str
    is_private: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------- documents


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    visible_to_client: bool
    uploaded_by_id: uuid.UUID | None
    created_at: datetime


class DownloadOut(BaseModel):
    url: str = Field(description="Short-lived presigned URL; do not store it")
    expires_in: int


# ---------------------------------------------------------------- hearings


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class HearingIn(BaseModel):
    scheduled_at: AwareDatetime
    court: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=500)

    _to_utc = field_validator("scheduled_at")(_utc)


class HearingUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: AwareDatetime | None = None
    court: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=500)
    status: HearingStatus | None = None
    notes: str | None = Field(default=None, max_length=10_000)

    @field_validator("scheduled_at")
    @classmethod
    def _to_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value else None


class HearingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scheduled_at: datetime
    court: str | None
    purpose: str | None
    status: HearingStatus
    notes: str | None
