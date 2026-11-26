"""THE access rule for chat rooms. Every chat operation goes through it, every time.

- case room: whoever can see the case (`cases.access.visible_to`: client, lead, members)
- booking room: the booking's client and advocate, while it is confirmed or completed

Nothing is cached per connection, so losing access to the case or booking means losing
access to the room on the very next message.
"""

import uuid

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.modules.bookings.models import Booking, BookingStatus
from app.modules.cases.access import require_open, visible_to
from app.modules.cases.models import Case, CaseMember
from app.modules.chat.models import ChatRoom
from app.modules.users.models import User

CHAT_BOOKING_STATUSES = (BookingStatus.CONFIRMED, BookingStatus.COMPLETED)


def room_visible_to(user: User) -> ColumnElement[bool]:
    cases = select(Case.id).where(visible_to(user))
    bookings = select(Booking.id).where(
        Booking.status.in_(CHAT_BOOKING_STATUSES),
        or_(Booking.client_id == user.id, Booking.advocate_id == user.id),
    )
    return or_(
        and_(ChatRoom.case_id.is_not(None), ChatRoom.case_id.in_(cases)),
        and_(ChatRoom.booking_id.is_not(None), ChatRoom.booking_id.in_(bookings)),
    )


async def get_room_for(session: AsyncSession, user: User, room_id: uuid.UUID) -> ChatRoom:
    """The room if the user may use it right now; 404 otherwise (never 403)."""
    room = await session.scalar(
        select(ChatRoom).where(ChatRoom.id == room_id, room_visible_to(user))
    )
    if room is None:
        raise NotFoundError("Chat room not found")
    return room


async def require_writable(session: AsyncSession, room: ChatRoom) -> None:
    """A closed case keeps its history readable but takes no new messages."""
    if room.case_id is not None:
        case = await session.get(Case, room.case_id)
        if case is not None:
            require_open(case)


async def participant_ids(session: AsyncSession, room: ChatRoom) -> list[uuid.UUID]:
    """Active users who may use the room right now (the fan-out recipients)."""
    if room.case_id is not None:
        case = await session.get(Case, room.case_id)
        if case is None:
            return []
        members = select(CaseMember.advocate_id).where(CaseMember.case_id == case.id)
        ids = or_(User.id.in_([case.client_id, case.lead_advocate_id]), User.id.in_(members))
    else:
        booking = await session.get(Booking, room.booking_id)
        if booking is None or booking.status not in CHAT_BOOKING_STATUSES:
            return []
        ids = User.id.in_([booking.client_id, booking.advocate_id])
    rows = await session.scalars(select(User.id).where(ids, User.is_active.is_(True)))
    return list(rows.all())
