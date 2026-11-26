import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import SessionDep
from app.core.pagination import Page, PageDep, make_page
from app.modules.auth.deps import CurrentUser, require_roles
from app.modules.bookings import service
from app.modules.bookings.models import BookingStatus
from app.modules.bookings.schemas import (
    AvailabilityIn,
    AvailabilityOut,
    AvailabilityRuleOut,
    BookingCreateIn,
    BookingOut,
    CancelIn,
    RescheduleIn,
    SlotOut,
    SlotQuery,
    SlotsOut,
)
from app.modules.users.models import User, UserRole

AdvocateUser = Annotated[User, Depends(require_roles(UserRole.ADVOCATE))]
ClientUser = Annotated[User, Depends(require_roles(UserRole.CLIENT))]

availability_router = APIRouter(prefix="/advocates", tags=["bookings"])
router = APIRouter(prefix="/bookings", tags=["bookings"])


def _availability_out(timezone: str, rules: list[object]) -> AvailabilityOut:
    return AvailabilityOut(
        timezone=timezone, rules=[AvailabilityRuleOut.model_validate(r) for r in rules]
    )


@availability_router.get("/me/availability", response_model=AvailabilityOut)
async def get_availability(user: AdvocateUser, session: SessionDep) -> AvailabilityOut:
    timezone, rules = await service.get_availability(session, user)
    return _availability_out(timezone, list(rules))


@availability_router.put(
    "/me/availability",
    response_model=AvailabilityOut,
    description="Replaces the weekly schedule. Times are wall-clock times in the advocate's "
    "profile timezone. Existing bookings are kept.",
)
async def replace_availability(
    data: AvailabilityIn, user: AdvocateUser, session: SessionDep
) -> AvailabilityOut:
    timezone, rules = await service.replace_availability(session, user, data)
    return _availability_out(timezone, list(rules))


@availability_router.get(
    "/{advocate_id}/slots",
    response_model=SlotsOut,
    summary="Free slots (public)",
    description="Free slots in UTC, for `days` local dates starting at `start_date` in the "
    "advocate's timezone. Excludes slots that are booked, too soon, or beyond the horizon.",
)
async def list_slots(
    advocate_id: uuid.UUID, session: SessionDep, query: Annotated[SlotQuery, Query()]
) -> SlotsOut:
    timezone, slots = await service.list_free_slots(
        session, advocate_id, query.start_date, query.days
    )
    return SlotsOut(
        advocate_id=advocate_id,
        timezone=timezone,
        slots=[SlotOut(start_at=s.start, end_at=s.end) for s in slots],
    )


@router.post(
    "",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
    description="Paid consultations start as `pending_payment` and expire if unpaid; "
    "free ones (fee 0) are confirmed immediately.",
)
async def create_booking(
    data: BookingCreateIn, user: ClientUser, session: SessionDep
) -> BookingOut:
    booking = await service.create_booking(session, user, data)
    return (await service.to_booking_outs(session, [booking]))[0]


@router.get("", response_model=Page[BookingOut], summary="My bookings (client or advocate)")
async def list_bookings(
    user: CurrentUser,
    session: SessionDep,
    page: PageDep,
    status_filter: Annotated[BookingStatus | None, Query(alias="status")] = None,
) -> Page[BookingOut]:
    rows, total = await service.list_bookings(session, user, status_filter, page)
    return make_page(await service.to_booking_outs(session, rows), total, page)


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(booking_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> BookingOut:
    booking = await service.get_visible_booking(session, user, booking_id)
    return (await service.to_booking_outs(session, [booking]))[0]


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID, data: CancelIn, user: CurrentUser, session: SessionDep
) -> BookingOut:
    booking = await service.cancel_booking(session, user, booking_id, data.reason)
    return (await service.to_booking_outs(session, [booking]))[0]


@router.post("/{booking_id}/reschedule", response_model=BookingOut)
async def reschedule_booking(
    booking_id: uuid.UUID, data: RescheduleIn, user: CurrentUser, session: SessionDep
) -> BookingOut:
    booking = await service.reschedule_booking(session, user, booking_id, data.start_at)
    return (await service.to_booking_outs(session, [booking]))[0]


@router.post("/{booking_id}/complete", response_model=BookingOut)
async def complete_booking(
    booking_id: uuid.UUID, user: AdvocateUser, session: SessionDep
) -> BookingOut:
    booking = await service.complete_booking(session, user, booking_id)
    return (await service.to_booking_outs(session, [booking]))[0]
