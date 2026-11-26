import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.core.deps import SessionDep
from app.core.pagination import Page, PageDep, make_page
from app.modules.audit.service import AuditDep
from app.modules.auth.deps import require_roles
from app.modules.cases import service
from app.modules.cases.access import CaseRole
from app.modules.cases.models import CaseStatus
from app.modules.cases.schemas import (
    CaseCreateIn,
    CaseMemberAddIn,
    CaseOut,
    CaseSummaryOut,
    CaseUpdateIn,
    DocumentOut,
    DownloadOut,
    HearingIn,
    HearingOut,
    HearingUpdateIn,
    NoteIn,
    NoteOut,
    NoteUpdateIn,
)
from app.modules.users.models import User, UserRole

AdvocateUser = Annotated[User, Depends(require_roles(UserRole.ADVOCATE))]
CaseUser = Annotated[User, Depends(require_roles(UserRole.CLIENT, UserRole.ADVOCATE))]

router = APIRouter(prefix="/cases", tags=["cases"])


@router.post(
    "",
    response_model=CaseOut,
    status_code=status.HTTP_201_CREATED,
    description="Opened by the advocate from one of their confirmed/completed bookings; "
    "the client is taken from that booking.",
)
async def create_case(
    data: CaseCreateIn, user: AdvocateUser, session: SessionDep, audit: AuditDep
) -> CaseOut:
    case = await service.create_case(session, user, data, audit)
    return await service.to_case_out(session, case, CaseRole.LEAD)


@router.get("", response_model=Page[CaseSummaryOut], summary="Cases I'm part of")
async def list_cases(
    user: CaseUser,
    session: SessionDep,
    page: PageDep,
    status_filter: Annotated[CaseStatus | None, Query(alias="status")] = None,
) -> Page[CaseSummaryOut]:
    rows, total = await service.list_cases(session, user, status_filter, page)
    return make_page(rows, total, page)


@router.get("/{case_id}", response_model=CaseOut, description="Audited (case.viewed).")
async def get_case(
    case_id: uuid.UUID, user: CaseUser, session: SessionDep, audit: AuditDep
) -> CaseOut:
    case, role = await service.view_case(session, user, case_id, audit)
    return await service.to_case_out(session, case, role)


@router.patch("/{case_id}", response_model=CaseOut, description="Lead advocate only.")
async def update_case(
    case_id: uuid.UUID, data: CaseUpdateIn, user: CaseUser, session: SessionDep, audit: AuditDep
) -> CaseOut:
    case, role = await service.update_case(session, user, case_id, data, audit)
    return await service.to_case_out(session, case, role)


# ---------------------------------------------------------------- members


@router.post("/{case_id}/members", response_model=CaseOut, status_code=status.HTTP_201_CREATED)
async def add_member(
    case_id: uuid.UUID,
    data: CaseMemberAddIn,
    user: CaseUser,
    session: SessionDep,
    audit: AuditDep,
) -> CaseOut:
    case, role = await service.add_member(session, user, case_id, data, audit)
    return await service.to_case_out(session, case, role)


@router.delete("/{case_id}/members/{advocate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    case_id: uuid.UUID,
    advocate_id: uuid.UUID,
    user: CaseUser,
    session: SessionDep,
    audit: AuditDep,
) -> None:
    await service.remove_member(session, user, case_id, advocate_id, audit)


# ---------------------------------------------------------------- notes


@router.get("/{case_id}/notes", response_model=list[NoteOut])
async def list_notes(case_id: uuid.UUID, user: CaseUser, session: SessionDep) -> list[NoteOut]:
    return [NoteOut.model_validate(n) for n in await service.list_notes(session, user, case_id)]


@router.post("/{case_id}/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def add_note(
    case_id: uuid.UUID, data: NoteIn, user: CaseUser, session: SessionDep
) -> NoteOut:
    return NoteOut.model_validate(await service.add_note(session, user, case_id, data))


@router.patch("/{case_id}/notes/{note_id}", response_model=NoteOut)
async def update_note(
    case_id: uuid.UUID, note_id: uuid.UUID, data: NoteUpdateIn, user: CaseUser, session: SessionDep
) -> NoteOut:
    note = await service.update_note(session, user, case_id, note_id, data.body)
    return NoteOut.model_validate(note)


@router.delete("/{case_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    case_id: uuid.UUID, note_id: uuid.UUID, user: CaseUser, session: SessionDep
) -> None:
    await service.delete_note(session, user, case_id, note_id)


# ---------------------------------------------------------------- documents


@router.get("/{case_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    case_id: uuid.UUID, user: CaseUser, session: SessionDep
) -> list[DocumentOut]:
    docs = await service.list_documents(session, user, case_id)
    return [DocumentOut.model_validate(d) for d in docs]


@router.post(
    "/{case_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    description="Multipart upload. PDF, PNG, JPEG, DOCX or TXT, detected from the content; "
    "max size `CASE_DOCUMENT_MAX_MB`. `visible_to_client` is ignored for client uploads.",
)
async def upload_document(
    case_id: uuid.UUID,
    user: CaseUser,
    session: SessionDep,
    audit: AuditDep,
    file: Annotated[UploadFile, File()],
    visible_to_client: Annotated[bool, Form()] = True,
) -> DocumentOut:
    document = await service.upload_document(
        session, user, case_id, file, audit, visible_to_client=visible_to_client
    )
    return DocumentOut.model_validate(document)


@router.get("/{case_id}/documents/{document_id}/download", response_model=DownloadOut)
async def download_document(
    case_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CaseUser,
    session: SessionDep,
    audit: AuditDep,
) -> DownloadOut:
    url, ttl = await service.document_download_url(session, user, case_id, document_id, audit)
    return DownloadOut(url=url, expires_in=ttl)


@router.delete("/{case_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    case_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CaseUser,
    session: SessionDep,
    audit: AuditDep,
) -> None:
    await service.delete_document(session, user, case_id, document_id, audit)


# ---------------------------------------------------------------- hearings


@router.get("/{case_id}/hearings", response_model=list[HearingOut])
async def list_hearings(
    case_id: uuid.UUID, user: CaseUser, session: SessionDep
) -> list[HearingOut]:
    return [
        HearingOut.model_validate(h) for h in await service.list_hearings(session, user, case_id)
    ]


@router.post("/{case_id}/hearings", response_model=HearingOut, status_code=status.HTTP_201_CREATED)
async def add_hearing(
    case_id: uuid.UUID, data: HearingIn, user: CaseUser, session: SessionDep
) -> HearingOut:
    return HearingOut.model_validate(await service.add_hearing(session, user, case_id, data))


@router.patch("/{case_id}/hearings/{hearing_id}", response_model=HearingOut)
async def update_hearing(
    case_id: uuid.UUID,
    hearing_id: uuid.UUID,
    data: HearingUpdateIn,
    user: CaseUser,
    session: SessionDep,
) -> HearingOut:
    hearing = await service.update_hearing(session, user, case_id, hearing_id, data)
    return HearingOut.model_validate(hearing)
