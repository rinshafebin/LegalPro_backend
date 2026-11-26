import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.modules.users.models import UserRole


class ProfileSummary(BaseModel):
    full_name: str
    phone: str | None = None
    # Advocate-only fields
    bar_council_id: str | None = None
    is_verified: bool | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: UserRole
    email_verified: bool
    mfa_enabled: bool
    created_at: datetime
    profile: ProfileSummary | None = None


class MeUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    phone: Annotated[str, Field(pattern=r"^\+?[0-9 ()-]{7,20}$")] | None = None
