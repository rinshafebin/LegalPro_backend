"""Cases, members, notes, documents and hearings.

Every function that touches a case goes through `access.get_case_for` (single access rule).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import UploadFile
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.email import enqueue_email
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.core.pagination import PageParams, paginate
from app.core.storage import get_storage
from app.modules.advocates.models import AdvocateProfile
from app.modules.bookings.emails import format_local
from app.modules.bookings.models import Booking, BookingStatus
from app.modules.cases import files
from app.modules.cases.access import (
    ADVOCATE_ROLES,
    CaseRole,
    get_case_for,
    require_open,
    require_role,
    visible_to,
)
from app.modules.cases.models import (
    Case,
    CaseDocument,
    CaseMember,
    CaseNote,
    CaseStatus,
    Hearing,
    HearingStatus,
    case_number_seq,
)
from app.modules.cases.schemas import (
    CaseCreateIn,
    CaseMemberAddIn,
    CaseMemberOut,
    CaseOut,
    CaseSummaryOut,
    CaseUpdateIn,
    HearingIn,
    HearingUpdateIn,
    NoteIn,
    PersonOut,
)
from app.modules.clients.models import ClientProfile
from app.modules.users.models import User
from app.workers.queue import get_job_queue

logger = get_logger(__name__)

HEARING_REMINDERS = (("24h", timedelta(hours=24)), ("1h", timedelta(hours=1)))


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- cases


async def create_case(session: AsyncSession, advocate: User, data: CaseCreateIn) -> Case:
    """A case is opened by the advocate from one of their own consultations.

    Tying it to a booking means an advocate can't open cases for arbitrary clients.
    """
    profile = await session.get(AdvocateProfile, advocate.id)
    if profile is None or not profile.is_verified:
        raise ForbiddenError("Only verified advocates can open cases")
    booking = await session.scalar(
        select(Booking).where(Booking.id == data.booking_id, Booking.advocate_id == advocate.id)
    )
    if booking is None:
        raise NotFoundError("Booking not found")
    if booking.status not in (BookingStatus.CONFIRMED, BookingStatus.COMPLETED):
        raise ConflictError("A case can only be opened from a confirmed or completed booking")

    number = await session.scalar(select(case_number_seq.next_value()))
    case = Case(
        case_number=f"CASE-{utcnow().year}-{int(number or 0):06d}",
        title=data.title.strip(),
        description=data.description,
        client_id=booking.client_id,
        lead_advocate_id=advocate.id,
        booking_id=booking.id,
        priority=data.priority,
        court_name=data.court_name,
    )
    session.add(case)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("A case already exists for this booking") from exc
    logger.info("case_created", case_id=str(case.id), case_number=case.case_number)
    return case


def _visible_cases(user: User) -> Select[Case]:
    return select(Case).where(visible_to(user))


async def list_cases(
    session: AsyncSession, user: User, status: CaseStatus | None, page: PageParams
) -> tuple[list[CaseSummaryOut], int]:
    stmt = _visible_cases(user)
    if status is not None:
        stmt = stmt.where(Case.status == status)
    rows, total = await paginate(session, stmt.order_by(Case.updated_at.desc(), Case.id), page)
    return [
        CaseSummaryOut(
            id=c.id,
            case_number=c.case_number,
            title=c.title,
            status=c.status,
            priority=c.priority,
            my_role=_role_of(c, user),
            updated_at=c.updated_at,
        )
        for c in rows
    ], total


def _role_of(case: Case, user: User) -> CaseRole:
    if case.client_id == user.id:
        return CaseRole.CLIENT
    if case.lead_advocate_id == user.id:
        return CaseRole.LEAD
    return CaseRole.MEMBER


async def _members(session: AsyncSession, case_id: uuid.UUID) -> list[CaseMember]:
    stmt = select(CaseMember).where(CaseMember.case_id == case_id).order_by(CaseMember.added_at)
    return list((await session.scalars(stmt)).all())


async def to_case_out(session: AsyncSession, case: Case, role: CaseRole) -> CaseOut:
    members = await _members(session, case.id)
    advocate_ids = [case.lead_advocate_id, *(m.advocate_id for m in members)]
    names = dict(
        (
            await session.execute(
                select(AdvocateProfile.user_id, AdvocateProfile.full_name).where(
                    AdvocateProfile.user_id.in_(advocate_ids)
                )
            )
        ).all()
    )
    client = await session.get(ClientProfile, case.client_id)
    return CaseOut(
        id=case.id,
        case_number=case.case_number,
        title=case.title,
        status=case.status,
        priority=case.priority,
        my_role=role,
        updated_at=case.updated_at,
        description=case.description,
        result=case.result,
        court_name=case.court_name,
        client=PersonOut(id=case.client_id, full_name=client.full_name if client else ""),
        lead_advocate=PersonOut(
            id=case.lead_advocate_id, full_name=names.get(case.lead_advocate_id, "")
        ),
        members=[
            CaseMemberOut(
                id=m.advocate_id,
                full_name=names.get(m.advocate_id, ""),
                role=m.role,
                added_at=m.added_at,
            )
            for m in members
        ],
        booking_id=case.booking_id,
        created_at=case.created_at,
        closed_at=case.closed_at,
    )


async def update_case(
    session: AsyncSession, user: User, case_id: uuid.UUID, data: CaseUpdateIn
) -> tuple[Case, CaseRole]:
    case, role = await get_case_for(session, user, case_id, for_update=True)
    require_role(role, CaseRole.LEAD, action="edit case details")
    changes = data.model_dump(exclude_unset=True)
    if case.status is CaseStatus.CLOSED and changes.get("status") in (None, CaseStatus.CLOSED):
        require_open(case)  # a closed case only accepts being reopened
    for field, value in changes.items():
        if field == "title" and value is None:
            continue
        setattr(case, field, value)
    if "status" in changes:
        case.closed_at = utcnow() if case.status is CaseStatus.CLOSED else None
    await session.commit()
    return case, role


# ---------------------------------------------------------------- members


async def add_member(
    session: AsyncSession, user: User, case_id: uuid.UUID, data: CaseMemberAddIn
) -> tuple[Case, CaseRole]:
    case, role = await get_case_for(session, user, case_id)
    require_role(role, CaseRole.LEAD, action="manage members")
    require_open(case)
    if data.advocate_id == case.lead_advocate_id:
        raise BadRequestError("The lead advocate is already on the case")
    eligible = await session.scalar(
        select(AdvocateProfile.user_id)
        .join(User, User.id == AdvocateProfile.user_id)
        .where(
            AdvocateProfile.user_id == data.advocate_id,
            AdvocateProfile.is_verified.is_(True),
            User.is_active.is_(True),
        )
    )
    if eligible is None:
        raise BadRequestError("Members must be verified advocates")
    session.add(CaseMember(case_id=case.id, advocate_id=data.advocate_id, role=data.role))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("Advocate is already a member of this case") from exc
    return case, role


async def remove_member(
    session: AsyncSession, user: User, case_id: uuid.UUID, advocate_id: uuid.UUID
) -> None:
    """The lead removes anyone; a member can remove themselves."""
    case, role = await get_case_for(session, user, case_id)
    if role is not CaseRole.LEAD and user.id != advocate_id:
        raise ForbiddenError("Only the lead advocate can remove other members")
    member = await session.get(CaseMember, (case.id, advocate_id))
    if member is None:
        raise NotFoundError("Advocate is not a member of this case")
    await session.delete(member)
    await session.commit()


# ---------------------------------------------------------------- notes


async def list_notes(session: AsyncSession, user: User, case_id: uuid.UUID) -> list[CaseNote]:
    case, role = await get_case_for(session, user, case_id)
    stmt = select(CaseNote).where(CaseNote.case_id == case.id)
    if role is CaseRole.CLIENT:
        stmt = stmt.where(CaseNote.is_private.is_(False))
    return list((await session.scalars(stmt.order_by(CaseNote.created_at))).all())


async def add_note(session: AsyncSession, user: User, case_id: uuid.UUID, data: NoteIn) -> CaseNote:
    case, role = await get_case_for(session, user, case_id)
    require_open(case)
    if data.is_private and role is CaseRole.CLIENT:
        raise ForbiddenError("Clients can't create private notes")
    note = CaseNote(case_id=case.id, author_id=user.id, body=data.body, is_private=data.is_private)
    session.add(note)
    await session.commit()
    return note


async def _own_note(
    session: AsyncSession, user: User, case_id: uuid.UUID, note_id: uuid.UUID
) -> CaseNote:
    case, role = await get_case_for(session, user, case_id)
    note = await session.scalar(
        select(CaseNote).where(CaseNote.id == note_id, CaseNote.case_id == case.id)
    )
    if note is None or (note.is_private and role is CaseRole.CLIENT):
        raise NotFoundError("Note not found")
    if note.author_id != user.id:
        raise ForbiddenError("You can only change your own notes")
    require_open(case)
    return note


async def update_note(
    session: AsyncSession, user: User, case_id: uuid.UUID, note_id: uuid.UUID, body: str
) -> CaseNote:
    note = await _own_note(session, user, case_id, note_id)
    note.body = body
    await session.commit()
    return note


async def delete_note(
    session: AsyncSession, user: User, case_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    note = await _own_note(session, user, case_id, note_id)
    await session.delete(note)
    await session.commit()


# ---------------------------------------------------------------- documents


async def list_documents(
    session: AsyncSession, user: User, case_id: uuid.UUID
) -> list[CaseDocument]:
    case, role = await get_case_for(session, user, case_id)
    stmt = select(CaseDocument).where(CaseDocument.case_id == case.id)
    if role is CaseRole.CLIENT:
        stmt = stmt.where(CaseDocument.visible_to_client.is_(True))
    return list((await session.scalars(stmt.order_by(CaseDocument.created_at))).all())


async def upload_document(
    session: AsyncSession,
    user: User,
    case_id: uuid.UUID,
    upload: UploadFile,
    *,
    visible_to_client: bool,
) -> CaseDocument:
    case, role = await get_case_for(session, user, case_id)
    require_open(case)
    max_bytes = get_settings().case_document_max_mb * 1024 * 1024
    data = await files.read_limited(upload, max_bytes)
    content_type = files.detect_type(data, upload.filename or "")

    doc_id = uuid.uuid4()
    key = f"cases/{case.id}/{doc_id}{files.ALLOWED_TYPES[content_type]}"
    await get_storage().put(key, data, content_type)  # upload first; DB row only if it worked
    document = CaseDocument(
        id=doc_id,
        case_id=case.id,
        uploaded_by_id=user.id,
        storage_key=key,
        filename=files.safe_filename(upload.filename, content_type),
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        # Anything the client uploads is obviously visible to them.
        visible_to_client=True if role is CaseRole.CLIENT else visible_to_client,
    )
    session.add(document)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await get_storage().delete(key)
        raise
    logger.info("case_document_uploaded", case_id=str(case.id), document_id=str(doc_id))
    return document


async def _visible_document(
    session: AsyncSession, user: User, case_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[Case, CaseRole, CaseDocument]:
    case, role = await get_case_for(session, user, case_id)
    document = await session.scalar(
        select(CaseDocument).where(CaseDocument.id == document_id, CaseDocument.case_id == case.id)
    )
    if document is None or (role is CaseRole.CLIENT and not document.visible_to_client):
        raise NotFoundError("Document not found")
    return case, role, document


async def document_download_url(
    session: AsyncSession, user: User, case_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[str, int]:
    _, _, document = await _visible_document(session, user, case_id, document_id)
    ttl = get_settings().s3_presign_ttl_seconds
    url = await get_storage().presigned_get(
        document.storage_key,
        filename=document.filename,
        content_type=document.content_type,
        ttl_seconds=ttl,
    )
    logger.info("case_document_download", document_id=str(document.id), user_id=str(user.id))
    return url, ttl


async def delete_document(
    session: AsyncSession, user: User, case_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    case, role, document = await _visible_document(session, user, case_id, document_id)
    if role is not CaseRole.LEAD and document.uploaded_by_id != user.id:
        raise ForbiddenError("Only the uploader or the lead advocate can delete a document")
    require_open(case)
    key = document.storage_key
    await session.delete(document)
    await session.commit()
    await get_storage().delete(key)


# ---------------------------------------------------------------- hearings


def hearing_reminder_job_id(hearing_id: uuid.UUID, kind: str, at: datetime) -> str:
    return f"hearing:{hearing_id}:reminder:{kind}:{int(at.timestamp())}"


async def _schedule_hearing_reminders(hearing: Hearing) -> None:
    now = utcnow()
    for kind, lead in HEARING_REMINDERS:
        run_at = hearing.scheduled_at - lead
        if run_at > now:
            await get_job_queue().enqueue(
                "send_hearing_reminder",
                job_id=hearing_reminder_job_id(hearing.id, kind, hearing.scheduled_at),
                defer_until=run_at,
                hearing_id=str(hearing.id),
                kind=kind,
                start_ts=int(hearing.scheduled_at.timestamp()),
            )


async def _abort_hearing_reminders(hearing_id: uuid.UUID, at: datetime) -> None:
    for kind, _ in HEARING_REMINDERS:
        await get_job_queue().abort(hearing_reminder_job_id(hearing_id, kind, at))


async def list_hearings(session: AsyncSession, user: User, case_id: uuid.UUID) -> list[Hearing]:
    case, _ = await get_case_for(session, user, case_id)
    stmt = select(Hearing).where(Hearing.case_id == case.id).order_by(Hearing.scheduled_at)
    return list((await session.scalars(stmt)).all())


async def add_hearing(
    session: AsyncSession, user: User, case_id: uuid.UUID, data: HearingIn
) -> Hearing:
    case, role = await get_case_for(session, user, case_id)
    require_role(role, *ADVOCATE_ROLES, action="schedule hearings")
    require_open(case)
    if data.scheduled_at <= utcnow():
        raise BadRequestError("Hearings must be scheduled in the future")
    hearing = Hearing(
        case_id=case.id, scheduled_at=data.scheduled_at, court=data.court, purpose=data.purpose
    )
    session.add(hearing)
    await session.commit()
    await _schedule_hearing_reminders(hearing)
    return hearing


async def update_hearing(
    session: AsyncSession,
    user: User,
    case_id: uuid.UUID,
    hearing_id: uuid.UUID,
    data: HearingUpdateIn,
) -> Hearing:
    case, role = await get_case_for(session, user, case_id)
    require_role(role, *ADVOCATE_ROLES, action="update hearings")
    require_open(case)
    hearing = await session.scalar(
        select(Hearing).where(Hearing.id == hearing_id, Hearing.case_id == case.id)
    )
    if hearing is None:
        raise NotFoundError("Hearing not found")
    changes = data.model_dump(exclude_unset=True)
    if changes.get("scheduled_at") is not None and changes["scheduled_at"] <= utcnow():
        raise BadRequestError("Hearings must be scheduled in the future")

    old_at, old_status = hearing.scheduled_at, hearing.status
    for field, value in changes.items():
        if field in {"scheduled_at", "status"} and value is None:
            continue
        setattr(hearing, field, value)
    await session.commit()

    if hearing.scheduled_at != old_at or hearing.status is not old_status:
        await _abort_hearing_reminders(hearing.id, old_at)
        if hearing.status is HearingStatus.SCHEDULED:
            await _schedule_hearing_reminders(hearing)
    return hearing


async def send_hearing_reminder(
    session: AsyncSession, hearing_id: uuid.UUID, kind: str, start_ts: int
) -> int:
    """Job body. Returns the number of reminders queued (0 if the hearing changed)."""
    hearing = await session.get(Hearing, hearing_id)
    if (
        hearing is None
        or hearing.status is not HearingStatus.SCHEDULED
        or int(hearing.scheduled_at.timestamp()) != start_ts
    ):
        return 0
    case = await session.get(Case, hearing.case_id)
    if case is None or case.status is CaseStatus.CLOSED:
        return 0
    members = await _members(session, case.id)
    user_ids = [case.client_id, case.lead_advocate_id, *(m.advocate_id for m in members)]
    users = (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
    lead = await session.get(AdvocateProfile, case.lead_advocate_id)
    when = format_local(hearing.scheduled_at, lead.timezone if lead else "Asia/Kolkata")
    subject = f"Reminder: hearing in {kind} ({case.case_number})"
    body = (
        f"Case {case.case_number}: {case.title}\n"
        f"Hearing: {when}\n"
        f"Court: {hearing.court or 'TBD'}\n"
        f"Purpose: {hearing.purpose or '-'}\n"
    )
    for u in users:
        if u.is_active:
            await enqueue_email(u.email, subject, body)
    return len(users)
