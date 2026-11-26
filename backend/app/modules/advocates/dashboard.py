"""GET /advocates/me/dashboard: plain queries, no caching (yet)."""

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bookings.models import Booking, BookingStatus
from app.modules.cases.access import visible_to
from app.modules.cases.models import Case, CaseStatus, Hearing, HearingStatus
from app.modules.clients.models import ClientProfile
from app.modules.payments.models import Payment, RefundStatus
from app.modules.users.models import User

HEARING_WINDOW = timedelta(days=7)
UPCOMING_LIMIT = 20
REFUND_STATES = (RefundStatus.REQUESTED, RefundStatus.FAILED, RefundStatus.PENDING_REVIEW)


class DashboardHearing(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID
    case_number: str
    scheduled_at: datetime
    court: str | None


class DashboardBooking(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    client_name: str
    start_at: datetime
    end_at: datetime


class DashboardOut(BaseModel):
    cases_by_status: dict[CaseStatus, int]
    upcoming_hearings: list[DashboardHearing]  # next 7 days, cases I'm on
    upcoming_bookings: list[DashboardBooking]  # confirmed, soonest first (max 20)
    upcoming_bookings_total: int
    pending_payments: int  # my bookings still waiting for the client to pay
    refunds: dict[RefundStatus, int]  # requested / failed / pending_review on my bookings


async def build(session: AsyncSession, user: User) -> DashboardOut:
    now = datetime.now(UTC)
    counts = dict(
        (
            await session.execute(
                select(Case.status, func.count()).where(visible_to(user)).group_by(Case.status)
            )
        ).all()
    )

    hearings = (
        await session.execute(
            select(Hearing, Case.case_number)
            .join(Case, Case.id == Hearing.case_id)
            .where(
                visible_to(user),
                Case.status != CaseStatus.CLOSED,
                Hearing.status == HearingStatus.SCHEDULED,
                Hearing.scheduled_at >= now,
                Hearing.scheduled_at < now + HEARING_WINDOW,
            )
            .order_by(Hearing.scheduled_at)
            .limit(50)
        )
    ).all()

    upcoming = select(Booking).where(
        Booking.advocate_id == user.id,
        Booking.status == BookingStatus.CONFIRMED,
        Booking.start_at >= now,
    )
    bookings = (
        await session.execute(
            upcoming.add_columns(ClientProfile.full_name)
            .outerjoin(ClientProfile, ClientProfile.user_id == Booking.client_id)
            .order_by(Booking.start_at)
            .limit(UPCOMING_LIMIT)
        )
    ).all()
    upcoming_total = await session.scalar(select(func.count()).select_from(upcoming.subquery()))

    pending = await session.scalar(
        select(func.count())
        .select_from(Booking)
        .where(
            Booking.advocate_id == user.id,
            Booking.status == BookingStatus.PENDING_PAYMENT,
            Booking.expires_at > now,
        )
    )
    refunds = dict(
        (
            await session.execute(
                select(Payment.refund_status, func.count())
                .join(Booking, Booking.id == Payment.booking_id)
                .where(Booking.advocate_id == user.id, Payment.refund_status.in_(REFUND_STATES))
                .group_by(Payment.refund_status)
            )
        ).all()
    )

    return DashboardOut(
        cases_by_status={s: int(counts.get(s, 0)) for s in CaseStatus},
        upcoming_hearings=[
            DashboardHearing(
                id=h.id,
                case_id=h.case_id,
                case_number=number,
                scheduled_at=h.scheduled_at,
                court=h.court,
            )
            for h, number in hearings
        ],
        upcoming_bookings=[
            DashboardBooking(
                id=b.id,
                client_id=b.client_id,
                client_name=name or "",
                start_at=b.start_at,
                end_at=b.end_at,
            )
            for b, name in bookings
        ],
        upcoming_bookings_total=int(upcoming_total or 0),
        pending_payments=int(pending or 0),
        refunds={s: int(refunds.get(s, 0)) for s in REFUND_STATES},
    )
