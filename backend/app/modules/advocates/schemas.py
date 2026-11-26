import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

Slug = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)]
Phone = Annotated[str, Field(pattern=r"^\+?[0-9 ()-]{7,20}$")]
Gender = Literal["male", "female", "other", "prefer_not_to_say"]
Fee = Annotated[Decimal, Field(ge=0, le=1_000_000, max_digits=10, decimal_places=2)]


def _normalize_languages(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        name = " ".join(value.split()).lower()
        if not 2 <= len(name) <= 40:
            raise ValueError("Each language must be 2-40 characters")
        seen.setdefault(name, None)
    return list(seen)


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown IANA timezone, e.g. 'Asia/Kolkata'") from exc
    return value


def _check_enrollment_year(value: int | None) -> int | None:
    if value is not None and not 1950 <= value <= datetime.now(UTC).year:
        raise ValueError("Enrollment year is out of range")
    return value


Languages = Annotated[list[str], Field(max_length=10), AfterValidator(_normalize_languages)]
EnrollmentYear = Annotated[int | None, AfterValidator(_check_enrollment_year)]
TimeZoneName = Annotated[str, Field(max_length=64), AfterValidator(_check_timezone)]


# ---------------------------------------------------------------- specializations


class SpecializationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str


class SpecializationCreateIn(BaseModel):
    slug: Slug
    name: str = Field(min_length=2, max_length=120)


# ---------------------------------------------------------------- profiles


class AdvocatePublicOut(BaseModel):
    """What anyone can see. No phone, email or street address."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(validation_alias="user_id", description="The advocate's User.id")
    full_name: str
    bio: str | None
    gender: str | None
    bar_council_id: str
    enrollment_year: int | None
    experience_years: int
    languages: list[str]
    consultation_fee: Decimal | None
    timezone: str
    city: str | None
    state: str | None
    specializations: list[SpecializationOut]
    avatar_url: str | None = Field(
        default=None, description="Short-lived signed URL (S3_PRESIGN_TTL_SECONDS)"
    )


class AdvocateMeOut(AdvocatePublicOut):
    phone: str | None
    address_line1: str | None
    address_line2: str | None
    pincode: str | None
    is_verified: bool
    verified_at: datetime | None


class AdvocateMeUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    phone: Phone | None = None
    gender: Gender | None = None
    bio: str | None = Field(default=None, max_length=2000)
    enrollment_year: EnrollmentYear = None
    experience_years: int | None = Field(default=None, ge=0, le=80)
    languages: Languages | None = None
    consultation_fee: Fee | None = None
    timezone: TimeZoneName | None = None
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, min_length=2, max_length=120)
    state: str | None = Field(default=None, min_length=2, max_length=120)
    pincode: str | None = Field(default=None, pattern=r"^[A-Za-z0-9 -]{3,12}$")
    specialization_slugs: list[Slug] | None = Field(default=None, max_length=10)


class AdvocateSearchParams(BaseModel):
    q: str | None = Field(default=None, min_length=2, max_length=100, description="Name contains")
    city: str | None = Field(default=None, max_length=120)
    specialization: Slug | None = None
    language: str | None = Field(default=None, max_length=40)
    min_experience: int | None = Field(default=None, ge=0, le=80)
    max_experience: int | None = Field(default=None, ge=0, le=80)


# ---------------------------------------------------------------- admin


class AdminAdvocateOut(BaseModel):
    id: uuid.UUID
    email: str
    email_verified: bool
    full_name: str
    bar_council_id: str
    is_verified: bool
    verified_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------- teams


class TeamMemberOut(BaseModel):
    id: uuid.UUID = Field(description="The member's User.id")
    full_name: str
    joined_at: datetime | None = None


class TeamOut(BaseModel):
    id: uuid.UUID
    name: str
    lead: TeamMemberOut
    members: list[TeamMemberOut]
    created_at: datetime


class TeamCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    member_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class TeamUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=120)


class TeamMemberAddIn(BaseModel):
    advocate_id: uuid.UUID
