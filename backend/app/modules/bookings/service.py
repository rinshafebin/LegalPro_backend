import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.core.pagination import PageParams, paginate
from app.modules.advocates.models import AdvocateProfile
from app.modules.bookings import emails
from app.modules.bookings.models import ACTIVE_STATUSES, AvailabilityRule, Booking, BookingStatus
from app.modules.bookings.schemas import AvailabilityIn, BookingCreateIn, BookingOut, PartyOut
from app.modules.bookings.slots import Slot, free_slots, generate_slots, rules_overlap
from app.modules.chat.service import ensure_booking_room
from app.modules.clients.models import ClientProfile
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import notify
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.refunds import (
    Canceller,
    enqueue_refund,
    refundable_paise,
    request_refund,
)
from app.modules.users.models import User
from app.workers.queue import get_job_queue

logger = get_logger(__name__)

REMINDERS = (("24h", timedelta(hours=24)), ("1h", timedelta(hours=1)))


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- job ids
# Deterministic, so cancel/reschedule can abort exactly the jobs that were scheduled.
# Reminder ids include the start time: a rescheduled booking gets fresh ids.


def expire_job_id(booking_id: uuid.UUID) -> str:
    return f"booking:{booking_id}:expire"


def reminder_job_id(booking_id: uuid.UUID, kind: str, start_at: datetime) -> str:
    return f"booking:{booking_id}:reminder:{kind}:{int(start_at.timestamp())}"


# ---------------------------------------------------------------- availability


async def _get_advocate_profile(session: AsyncSession, advocate_id: uuid.UUID) -> AdvocateProfile:
    profile = await session.get(AdvocateProfile, advocate_id)
    if profile is None:
        raise NotFoundError("Advocate profile not found")
    return profile


async def _rules(session: AsyncSession, advocate_id: uuid.UUID) -> list[AvailabilityRule]:
    stmt = (
        select(AvailabilityRule)
        .where(AvailabilityRule.advocate_id == advocate_id)
        .order_by(AvailabilityRule.weekday, AvailabilityRule.start_time)
    )
    return list((await session.scalars(stmt)).all())


async def get_availability(session: AsyncSession, user: User) -> tuple[str, list[AvailabilityRule]]:
    profile = await _get_advocate_profile(session, user.id)
    return profile.timezone, await _rules(session, user.id)


async def replace_availability(
    session: AsyncSession, user: User, data: AvailabilityIn
) -> tuple[str, list[AvailabilityRule]]:
    """Replace the whole weekly schedule. Existing bookings are not affected."""
    profile = await _get_advocate_profile(session, user.id)
    if rules_overlap(data.rules):
        raise BadRequestError("Availability windows on the same day overlap")
    await session.execute(delete(AvailabilityRule).where(AvailabilityRule.advocate_id == user.id))
    session.add_all(AvailabilityRule(advocate_id=user.id, **r.model_dump()) for r in data.rules)
    await session.commit()
    return profile.timezone, await _rules(session, user.id)


# ---------------------------------------------------------------- slots


async def _bookable_advocate(session: AsyncSession, advocate_id: uuid.UUID) -> AdvocateProfile:
    profile = await session.scalar(
        select(AdvocateProfile)
        .join(User, User.id == AdvocateProfile.user_id)
        .where(
            AdvocateProfile.user_id == advocate_id,
            AdvocateProfile.is_verified.is_(True),
            User.is_active.is_(True),
        )
    )
    if profile is None:
        raise NotFoundError("Advocate not found")
    return profile


def _booking_window(now: datetime) -> tuple[datetime, datetime]:
    s = get_settings()
    return (
        now + timedelta(minutes=s.booking_min_lead_minutes),
        now + timedelta(days=s.booking_horizon_days),
    )


async def _busy_intervals(
    session: AsyncSession,
    advocate_id: uuid.UUID,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    rows = await session.execute(
        select(Booking.start_at, Booking.end_at).where(
            Booking.advocate_id == advocate_id,
            Booking.status.in_(ACTIVE_STATUSES),
            Booking.start_at < end,
            Booking.end_at > start,
        )
    )
    return [(s, e) for s, e in rows]


async def list_free_slots(
    session: AsyncSession, advocate_id: uuid.UUID, start_date: date | None, days: int
) -> tuple[str, list[Slot]]:
    profile = await _bookable_advocate(session, advocate_id)
    tz = ZoneInfo(profile.timezone)
    now = utcnow()
    first_day = start_date or now.astimezone(tz).date()
    slots = generate_slots(await _rules(session, advocate_id), tz, first_day, days)
    if not slots:
        return profile.timezone, []
    busy = await _busy_intervals(session, advocate_id, slots[0].start, slots[-1].end)
    not_before, not_after = _booking_window(now)
    return profile.timezone, free_slots(slots, busy, not_before=not_before, not_after=not_after)


async def _resolve_slot(
    session: AsyncSession, profile: AdvocateProfile, start_at: datetime
) -> Slot:
    """Check `start_at` against the booking window and the advocate's real availability."""
    now = utcnow()
    not_before, not_after = _booking_window(now)
    if start_at < not_before:
        raise BadRequestError("This time is in the past or too soon to book", code="too_soon")
    if start_at > not_after:
        raise BadRequestError(
            f"Bookings can be made at most {get_settings().booking_horizon_days} days ahead",
            code="beyond_horizon",
        )
    tz = ZoneInfo(profile.timezone)
    local_day = start_at.astimezone(tz).date()
    rules = await _rules(session, profile.user_id)
    for slot in generate_slots(rules, tz, local_day - timedelta(days=1), 3):
        if slot.start == start_at:
            return slot
    raise BadRequestError("This time is not one of the advocate's slots", code="not_a_slot")


# ---------------------------------------------------------------- create


def _constraint_name(exc: IntegrityError) -> str | None:
    for err in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(err, "constraint_name", None)
        if name:
            return str(name)
    return None


async def _flush_or_conflict(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        name = _constraint_name(exc)
        if name == "ex_bookings_advocate_overlap":
            raise ConflictError(
                "This slot is no longer available", code="slot_unavailable"
            ) from exc
        if name == "ex_bookings_client_overlap":
            raise ConflictError(
                "You already have a booking that overlaps this time", code="client_overlap"
            ) from exc
        raise


async def create_booking(session: AsyncSession, client: User, data: BookingCreateIn) -> Booking:
    if not client.email_verified:
        raise ForbiddenError("Verify your email address before booking", code="email_not_verified")
    profile = await _bookable_advocate(session, data.advocate_id)
    if profile.consultation_fee is None:
        raise BadRequestError("This advocate is not accepting bookings yet", code="not_bookable")
    slot = await _resolve_slot(session, profile, data.start_at)

    now = utcnow()
    free = profile.consultation_fee == 0
    timeout = timedelta(minutes=get_settings().booking_payment_timeout_minutes)
    booking = Booking(
        client_id=client.id,
        advocate_id=profile.user_id,
        start_at=slot.start,
        end_at=slot.end,
        status=BookingStatus.CONFIRMED if free else BookingStatus.PENDING_PAYMENT,
        fee_amount=profile.consultation_fee,
        currency=get_settings().booking_currency,
        expires_at=None if free else now + timeout,
    )
    session.add(booking)
    await _flush_or_conflict(session)
    await session.commit()
    logger.info("booking_created", booking_id=str(booking.id), status=booking.status.value)

    if free:
        await after_confirmed(session, booking)
    else:
        assert booking.expires_at is not None  # noqa: S101 - set above
        await get_job_queue().enqueue(
            "expire_booking",
            job_id=expire_job_id(booking.id),
            defer_until=booking.expires_at,
            booking_id=str(booking.id),
        )
    return booking


# ---------------------------------------------------------------- notifications


@dataclass(frozen=True)
class _Parties:
    client: User
    client_name: str
    advocate: User
    advocate_name: str
    timezone: str


async def _parties(session: AsyncSession, booking: Booking) -> _Parties:
    client = await session.get(User, booking.client_id)
    advocate = await session.get(User, booking.advocate_id)
    client_profile = await session.get(ClientProfile, booking.client_id)
    advocate_profile = await session.get(AdvocateProfile, booking.advocate_id)
    if client is None or advocate is None or advocate_profile is None:  # FKs make this impossible
        raise RuntimeError(f"Booking {booking.id} references missing users")
    return _Parties(
        client=client,
        client_name=client_profile.full_name if client_profile else client.email,
        advocate=advocate,
        advocate_name=advocate_profile.full_name,
        timezone=advocate_profile.timezone,
    )


async def _tell(
    session: AsyncSession,
    user: User,
    type_: NotificationType,
    booking: Booking,
    email: tuple[str, str],
) -> None:
    await notify(
        session,
        user,
        type_,
        title=email[0],
        data={"booking_id": str(booking.id), "start_at": booking.start_at.isoformat()},
        email=email,
    )


async def _tell_both(
    session: AsyncSession,
    p: _Parties,
    type_: NotificationType,
    booking: Booking,
    for_client: tuple[str, str],
    for_advocate: tuple[str, str],
) -> None:
    await _tell(session, p.client, type_, booking, for_client)
    await _tell(session, p.advocate, type_, booking, for_advocate)


async def _schedule_reminders(booking: Booking) -> None:
    now = utcnow()
    for kind, lead in REMINDERS:
        run_at = booking.start_at - lead
        if run_at > now:
            await get_job_queue().enqueue(
                "send_booking_reminder",
                job_id=reminder_job_id(booking.id, kind, booking.start_at),
                defer_until=run_at,
                booking_id=str(booking.id),
                kind=kind,
                start_ts=int(booking.start_at.timestamp()),
            )


async def _abort_reminders(booking_id: uuid.UUID, start_at: datetime) -> None:
    for kind, _ in REMINDERS:
        await get_job_queue().abort(reminder_job_id(booking_id, kind, start_at))


async def after_confirmed(session: AsyncSession, booking: Booking) -> None:
    """Side effects once a booking is confirmed (after commit)."""
    await get_job_queue().abort(expire_job_id(booking.id))
    await _schedule_reminders(booking)
    await ensure_booking_room(session, booking.id)
    p = await _parties(session, booking)
    when = emails.format_local(booking.start_at, p.timezone)
    await _tell_both(
        session,
        p,
        NotificationType.BOOKING_CONFIRMED,
        booking,
        emails.confirmed(p.client_name, p.advocate_name, when),
        emails.confirmed(p.advocate_name, p.client_name, when),
    )


class ConfirmResult(StrEnum):
    CONFIRMED = "confirmed"  # newly confirmed now
    ALREADY = "already_confirmed"
    SLOT_LOST = "slot_lost"  # expired and someone else took the slot meanwhile
    NOT_CONFIRMABLE = "not_confirmable"  # cancelled or completed


async def confirm_after_payment(session: AsyncSession, booking: Booking) -> ConfirmResult:
    """Mark a booking paid/confirmed inside the caller's transaction (caller commits).

    A payment can land after the booking expired; if the slot is still free the booking
    is revived, otherwise the caller must flag the payment for a refund.
    """
    if booking.status is BookingStatus.CONFIRMED:
        return ConfirmResult.ALREADY
    if booking.status not in (BookingStatus.PENDING_PAYMENT, BookingStatus.EXPIRED):
        return ConfirmResult.NOT_CONFIRMABLE
    try:
        async with session.begin_nested():
            booking.status = BookingStatus.CONFIRMED
            booking.expires_at = None
            await session.flush()
    except IntegrityError:
        await session.refresh(booking)
        return ConfirmResult.SLOT_LOST
    return ConfirmResult.CONFIRMED


# ---------------------------------------------------------------- expiry


async def expire_booking(session: AsyncSession, booking_id: uuid.UUID) -> bool:
    """Expire one unpaid booking if its deadline has passed. Frees the slot."""
    result = await session.execute(
        update(Booking)
        .where(
            Booking.id == booking_id,
            Booking.status == BookingStatus.PENDING_PAYMENT,
            Booking.expires_at <= utcnow(),
        )
        .values(status=BookingStatus.EXPIRED)
        .returning(Booking.id)
    )
    expired = result.scalar_one_or_none() is not None
    await session.commit()
    if expired:
        logger.info("booking_expired", booking_id=str(booking_id))
    return expired


async def expire_stale_bookings(session: AsyncSession) -> int:
    """Safety-net sweep (cron) in case an expiry job was lost."""
    result = await session.execute(
        update(Booking)
        .where(Booking.status == BookingStatus.PENDING_PAYMENT, Booking.expires_at <= utcnow())
        .values(status=BookingStatus.EXPIRED)
        .returning(Booking.id)
    )
    count = len(result.all())
    await session.commit()
    if count:
        logger.info("bookings_expired_by_sweep", count=count)
    return count


# ---------------------------------------------------------------- read


async def get_visible_booking(session: AsyncSession, user: User, booking_id: uuid.UUID) -> Booking:
    """Only the booking's client and advocate can see it; to anyone else it doesn't exist."""
    booking = await session.scalar(
        select(Booking).where(
            Booking.id == booking_id,
            or_(Booking.client_id == user.id, Booking.advocate_id == user.id),
        )
    )
    if booking is None:
        raise NotFoundError("Booking not found")
    return booking


async def list_bookings(
    session: AsyncSession, user: User, status: BookingStatus | None, page: PageParams
) -> tuple[list[Booking], int]:
    stmt = select(Booking).where(or_(Booking.client_id == user.id, Booking.advocate_id == user.id))
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    stmt = stmt.order_by(Booking.start_at.desc(), Booking.id)
    return await paginate(session, stmt, page)


async def to_booking_outs(session: AsyncSession, bookings: list[Booking]) -> list[BookingOut]:
    client_ids = {b.client_id for b in bookings}
    advocate_ids = {b.advocate_id for b in bookings}
    clients = dict(
        (
            await session.execute(
                select(ClientProfile.user_id, ClientProfile.full_name).where(
                    ClientProfile.user_id.in_(client_ids)
                )
            )
        ).all()
    )
    advocates = {
        row.user_id: (row.full_name, row.timezone)
        for row in await session.execute(
            select(
                AdvocateProfile.user_id, AdvocateProfile.full_name, AdvocateProfile.timezone
            ).where(AdvocateProfile.user_id.in_(advocate_ids))
        )
    }
    return [
        BookingOut(
            id=b.id,
            status=b.status,
            client=PartyOut(id=b.client_id, full_name=clients.get(b.client_id, "")),
            advocate=PartyOut(id=b.advocate_id, full_name=advocates[b.advocate_id][0]),
            start_at=b.start_at,
            end_at=b.end_at,
            advocate_timezone=advocates[b.advocate_id][1],
            fee_amount=b.fee_amount,
            currency=b.currency,
            expires_at=b.expires_at,
            cancelled_at=b.cancelled_at,
            cancel_reason=b.cancel_reason,
            completed_at=b.completed_at,
            created_at=b.created_at,
        )
        for b in bookings
    ]


# ---------------------------------------------------------------- cancel / reschedule / complete


async def cancel_booking(
    session: AsyncSession, user: User, booking_id: uuid.UUID, reason: str | None
) -> Booking:
    """Either party can cancel an active booking; a paid booking is refunded per policy."""
    booking = await get_visible_booking(session, user, booking_id)
    if booking.status not in ACTIVE_STATUSES:
        raise ConflictError(f"A {booking.status.value} booking can't be cancelled")
    was_confirmed = booking.status is BookingStatus.CONFIRMED

    now = utcnow()
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = now
    booking.cancelled_by_id = user.id
    booking.cancel_reason = reason

    refund_payment_id: uuid.UUID | None = None
    payment = await session.scalar(
        select(Payment).where(Payment.booking_id == booking.id).with_for_update()
    )
    if payment is not None and payment.status is PaymentStatus.CAPTURED:
        by = Canceller.CLIENT if user.id == booking.client_id else Canceller.ADVOCATE
        amount = refundable_paise(payment.amount_paise, booking.start_at, now, by)
        if request_refund(payment, amount, reason=f"cancelled_by_{by.value}"):
            refund_payment_id = payment.id
    await session.commit()
    logger.info("booking_cancelled", booking_id=str(booking.id), by=str(user.id))

    if refund_payment_id is not None:
        await enqueue_refund(refund_payment_id)
    await get_job_queue().abort(expire_job_id(booking.id))
    await _abort_reminders(booking.id, booking.start_at)
    if was_confirmed:
        p = await _parties(session, booking)
        when = emails.format_local(booking.start_at, p.timezone)
        if user.id == booking.client_id:
            recipient, name, other = p.advocate, p.advocate_name, p.client_name
        else:
            recipient, name, other = p.client, p.client_name, p.advocate_name
        await _tell(
            session,
            recipient,
            NotificationType.BOOKING_CANCELLED,
            booking,
            emails.cancelled(name, other, when, reason),
        )
    return booking


async def reschedule_booking(
    session: AsyncSession, user: User, booking_id: uuid.UUID, new_start: datetime
) -> Booking:
    booking = await get_visible_booking(session, user, booking_id)
    if booking.status is not BookingStatus.CONFIRMED:
        raise ConflictError("Only confirmed bookings can be rescheduled; cancel and rebook instead")
    cutoff = timedelta(hours=get_settings().reschedule_cutoff_hours)
    if booking.start_at - utcnow() < cutoff:
        raise ConflictError(
            f"Bookings can only be rescheduled up to {cutoff.total_seconds() / 3600:g} hours "
            "before the start",
            code="reschedule_window_closed",
        )
    profile = await _get_advocate_profile(session, booking.advocate_id)
    slot = await _resolve_slot(session, profile, new_start)
    if slot.start == booking.start_at:
        return booking

    old_start = booking.start_at
    booking.start_at, booking.end_at = slot.start, slot.end
    await _flush_or_conflict(session)
    await session.commit()
    logger.info("booking_rescheduled", booking_id=str(booking.id))

    await _abort_reminders(booking.id, old_start)
    await _schedule_reminders(booking)
    p = await _parties(session, booking)
    when = emails.format_local(booking.start_at, p.timezone)
    await _tell_both(
        session,
        p,
        NotificationType.BOOKING_RESCHEDULED,
        booking,
        emails.rescheduled(p.client_name, p.advocate_name, when),
        emails.rescheduled(p.advocate_name, p.client_name, when),
    )
    return booking


async def complete_booking(session: AsyncSession, user: User, booking_id: uuid.UUID) -> Booking:
    booking = await get_visible_booking(session, user, booking_id)
    if booking.advocate_id != user.id:
        raise ForbiddenError("Only the advocate can mark a consultation as completed")
    if booking.status is not BookingStatus.CONFIRMED:
        raise ConflictError(f"A {booking.status.value} booking can't be completed")
    if booking.end_at > utcnow():
        raise ConflictError("The consultation hasn't ended yet")
    booking.status = BookingStatus.COMPLETED
    booking.completed_at = utcnow()
    await session.commit()
    return booking


# ---------------------------------------------------------------- reminder job


async def send_reminder(
    session: AsyncSession, booking_id: uuid.UUID, kind: str, start_ts: int
) -> bool:
    """Send a reminder unless the booking was cancelled or moved since it was scheduled."""
    booking = await session.get(Booking, booking_id)
    if (
        booking is None
        or booking.status is not BookingStatus.CONFIRMED
        or int(booking.start_at.timestamp()) != start_ts
    ):
        return False
    p = await _parties(session, booking)
    when = emails.format_local(booking.start_at, p.timezone)
    await _tell_both(
        session,
        p,
        NotificationType.BOOKING_REMINDER,
        booking,
        emails.reminder(p.client_name, p.advocate_name, when, kind),
        emails.reminder(p.advocate_name, p.client_name, when, kind),
    )
    return True
