import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.bookings.models import BookingStatus


class AvailabilityRuleIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday ... 6 = Sunday")
    start_time: time = Field(description="Local time in the advocate's timezone, e.g. 09:00")
    end_time: time
    slot_minutes: int = Field(default=30, ge=15, le=240)

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.start_time.tzinfo is not None or self.end_time.tzinfo is not None:
            raise ValueError("Use local wall-clock times without a UTC offset")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time (windows can't cross midnight)")
        return self


class AvailabilityRuleOut(AvailabilityRuleIn):
    model_config = ConfigDict(from_attributes=True)


class AvailabilityIn(BaseModel):
    rules: list[AvailabilityRuleIn] = Field(max_length=50)


class AvailabilityOut(BaseModel):
    timezone: str
    rules: list[AvailabilityRuleOut]


class SlotOut(BaseModel):
    start_at: datetime
    end_at: datetime


class SlotsOut(BaseModel):
    advocate_id: uuid.UUID
    timezone: str
    slots: list[SlotOut]


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class BookingCreateIn(BaseModel):
    advocate_id: uuid.UUID
    start_at: AwareDatetime = Field(description="Slot start; must include a UTC offset")

    _utc = field_validator("start_at")(_to_utc)


class RescheduleIn(BaseModel):
    start_at: AwareDatetime

    _utc = field_validator("start_at")(_to_utc)


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class PartyOut(BaseModel):
    id: uuid.UUID
    full_name: str


class BookingOut(BaseModel):
    id: uuid.UUID
    status: BookingStatus
    client: PartyOut
    advocate: PartyOut
    start_at: datetime
    end_at: datetime
    advocate_timezone: str
    fee_amount: Decimal
    currency: str
    expires_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    completed_at: datetime | None
    created_at: datetime


class SlotQuery(BaseModel):
    start_date: date | None = Field(
        default=None, description="First local date (advocate's zone). Defaults to today."
    )
    days: int = Field(default=7, ge=1, le=14)
