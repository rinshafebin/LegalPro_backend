import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.bookings.models import Booking, BookingStatus
from app.modules.users.models import UserRole
from app.workers import tasks
from tests.booking_support import session_factory
from tests.conftest import FakeJobQueue, FakeStorage
from tests.helpers import API, Actor, make_advocate, make_user

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


@dataclass
class CaseWorld:
    lead: Actor
    member: Actor
    outsider: Actor
    unverified: Actor
    client: Actor
    other_client: Actor
    booking_id: uuid.UUID
    case_id: str


async def _booking(
    session: AsyncSession,
    client_id: uuid.UUID,
    advocate_id: uuid.UUID,
    status: BookingStatus = BookingStatus.CONFIRMED,
    days: int = 3,
) -> uuid.UUID:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=days)
    booking = Booking(
        client_id=client_id,
        advocate_id=advocate_id,
        start_at=start,
        end_at=start + timedelta(minutes=30),
        status=status,
        fee_amount=Decimal("1000.00"),
        currency="INR",
    )
    session.add(booking)
    await session.commit()
    return booking.id


@pytest.fixture
async def cw(client: AsyncClient, db_session: AsyncSession) -> CaseWorld:
    lead = await make_advocate(db_session, "lead@example.com", full_name="Adv. Lead")
    member = await make_advocate(db_session, "member@example.com", full_name="Adv. Member")
    outsider = await make_advocate(db_session, "outsider@example.com", full_name="Adv. Out")
    unverified = await make_advocate(db_session, "unv@example.com", verified=False)
    owner = await make_user(db_session, UserRole.CLIENT, "owner@example.com", full_name="Owner")
    other = await make_user(db_session, UserRole.CLIENT, "other@example.com", full_name="Other")
    booking_id = await _booking(db_session, owner.id, lead.id)
    resp = await client.post(
        f"{API}/cases",
        headers=lead.headers,
        json={"booking_id": str(booking_id), "title": "Property dispute", "priority": "high"},
    )
    assert resp.status_code == 201, resp.text
    return CaseWorld(
        lead, member, outsider, unverified, owner, other, booking_id, resp.json()["id"]
    )


async def _add_member(client: AsyncClient, cw: CaseWorld) -> None:
    resp = await client.post(
        f"{API}/cases/{cw.case_id}/members",
        headers=cw.lead.headers,
        json={"advocate_id": str(cw.member.id), "role": "associate"},
    )
    assert resp.status_code == 201, resp.text


async def _upload(
    client: AsyncClient,
    actor: Actor,
    case_id: str,
    filename: str,
    data: bytes,
    *,
    visible: bool = True,
    declared: str = "application/octet-stream",
) -> Any:
    return await client.post(
        f"{API}/cases/{case_id}/documents",
        headers=actor.headers,
        files={"file": (filename, data, declared)},
        data={"visible_to_client": str(visible).lower()},
    )


# ---------------------------------------------------------------- creation


async def test_case_created_from_booking(client: AsyncClient, cw: CaseWorld) -> None:
    resp = await client.get(f"{API}/cases/{cw.case_id}", headers=cw.lead.headers)
    body = resp.json()
    assert re.fullmatch(rf"CASE-{datetime.now(UTC).year}-\d{{6}}", body["case_number"])
    assert body["client"] == {"id": str(cw.client.id), "full_name": "Owner"}
    assert body["lead_advocate"]["full_name"] == "Adv. Lead"
    assert body["my_role"] == "lead"
    assert body["status"] == "open"
    assert body["booking_id"] == str(cw.booking_id)


async def test_case_numbers_are_unique_and_sequential(
    client: AsyncClient, db_session: AsyncSession, cw: CaseWorld
) -> None:
    second_booking = await _booking(db_session, cw.other_client.id, cw.lead.id, days=4)
    resp = await client.post(
        f"{API}/cases",
        headers=cw.lead.headers,
        json={"booking_id": str(second_booking), "title": "Second matter"},
    )
    first = (await client.get(f"{API}/cases/{cw.case_id}", headers=cw.lead.headers)).json()
    a, b = int(first["case_number"][-6:]), int(resp.json()["case_number"][-6:])
    assert b == a + 1


async def test_case_creation_rules(
    client: AsyncClient, db_session: AsyncSession, cw: CaseWorld
) -> None:
    async def create(actor: Actor, booking_id: uuid.UUID) -> int:
        resp = await client.post(
            f"{API}/cases",
            headers=actor.headers,
            json={"booking_id": str(booking_id), "title": "Another"},
        )
        return resp.status_code

    assert await create(cw.lead, cw.booking_id) == 409  # one case per booking
    assert await create(cw.outsider, cw.booking_id) == 404  # not their booking
    assert await create(cw.client, cw.booking_id) == 403  # clients don't open cases
    pending = await _booking(
        db_session, cw.client.id, cw.lead.id, BookingStatus.PENDING_PAYMENT, days=5
    )
    assert await create(cw.lead, pending) == 409
    unverified_booking = await _booking(db_session, cw.client.id, cw.unverified.id, days=6)
    assert await create(cw.unverified, unverified_booking) == 403


# ---------------------------------------------------------------- permission matrix


@pytest.mark.parametrize("path", ["", "/notes", "/documents", "/hearings"])
async def test_permission_matrix(client: AsyncClient, cw: CaseWorld, path: str) -> None:
    await _add_member(client, cw)
    url = f"{API}/cases/{cw.case_id}{path}"
    for allowed in (cw.client, cw.lead, cw.member):
        assert (await client.get(url, headers=allowed.headers)).status_code == 200
    # Client B and unrelated advocates can't even learn the case exists.
    for denied in (cw.other_client, cw.outsider):
        assert (await client.get(url, headers=denied.headers)).status_code == 404


async def test_case_list_only_shows_own_cases(client: AsyncClient, cw: CaseWorld) -> None:
    await _add_member(client, cw)
    roles = {}
    for name in ("client", "lead", "member", "other_client", "outsider"):
        actor: Actor = getattr(cw, name)
        items = (await client.get(f"{API}/cases", headers=actor.headers)).json()["items"]
        roles[name] = [i["my_role"] for i in items]
    assert roles == {
        "client": ["client"],
        "lead": ["lead"],
        "member": ["member"],
        "other_client": [],
        "outsider": [],
    }


async def test_only_lead_edits_and_closed_case_is_read_only(
    client: AsyncClient, cw: CaseWorld
) -> None:
    await _add_member(client, cw)
    url = f"{API}/cases/{cw.case_id}"
    for actor in (cw.member, cw.client):
        resp = await client.patch(url, headers=actor.headers, json={"title": "Hijacked"})
        assert resp.status_code == 403

    closed = await client.patch(url, headers=cw.lead.headers, json={"status": "closed"})
    assert closed.json()["status"] == "closed"
    assert closed.json()["closed_at"] is not None

    note = await client.post(f"{url}/notes", headers=cw.lead.headers, json={"body": "late"})
    assert note.status_code == 409
    assert note.json()["error"]["code"] == "case_closed"
    edit = await client.patch(url, headers=cw.lead.headers, json={"title": "Still closed"})
    assert edit.status_code == 409

    reopened = await client.patch(url, headers=cw.lead.headers, json={"status": "open"})
    assert reopened.json()["closed_at"] is None


# ---------------------------------------------------------------- members


async def test_member_management(client: AsyncClient, cw: CaseWorld) -> None:
    url = f"{API}/cases/{cw.case_id}/members"
    bad = await client.post(
        url, headers=cw.lead.headers, json={"advocate_id": str(cw.unverified.id)}
    )
    assert bad.status_code == 400
    lead_again = await client.post(
        url, headers=cw.lead.headers, json={"advocate_id": str(cw.lead.id)}
    )
    assert lead_again.status_code == 400
    by_client = await client.post(
        url, headers=cw.client.headers, json={"advocate_id": str(cw.member.id)}
    )
    assert by_client.status_code == 403

    await _add_member(client, cw)
    dup = await client.post(url, headers=cw.lead.headers, json={"advocate_id": str(cw.member.id)})
    assert dup.status_code == 409

    assert (
        await client.delete(f"{url}/{cw.lead.id}", headers=cw.member.headers)
    ).status_code == 403
    assert (
        await client.delete(f"{url}/{cw.member.id}", headers=cw.member.headers)
    ).status_code == 204
    assert (
        await client.get(f"{API}/cases/{cw.case_id}", headers=cw.member.headers)
    ).status_code == 404


# ---------------------------------------------------------------- notes


async def test_private_notes_are_hidden_from_the_client(client: AsyncClient, cw: CaseWorld) -> None:
    await _add_member(client, cw)
    notes_url = f"{API}/cases/{cw.case_id}/notes"
    private = (
        await client.post(
            notes_url, headers=cw.lead.headers, json={"body": "Strategy", "is_private": True}
        )
    ).json()
    await client.post(notes_url, headers=cw.lead.headers, json={"body": "Hearing moved"})
    await client.post(notes_url, headers=cw.client.headers, json={"body": "Thanks"})

    client_view = [
        n["body"] for n in (await client.get(notes_url, headers=cw.client.headers)).json()
    ]
    member_view = [
        n["body"] for n in (await client.get(notes_url, headers=cw.member.headers)).json()
    ]
    assert client_view == ["Hearing moved", "Thanks"]
    assert member_view == ["Strategy", "Hearing moved", "Thanks"]

    # The client can't reach the private note by id either.
    resp = await client.patch(
        f"{notes_url}/{private['id']}", headers=cw.client.headers, json={"body": "x"}
    )
    assert resp.status_code == 404
    forbidden = await client.post(
        notes_url, headers=cw.client.headers, json={"body": "secret", "is_private": True}
    )
    assert forbidden.status_code == 403


async def test_only_authors_edit_notes(client: AsyncClient, cw: CaseWorld) -> None:
    await _add_member(client, cw)
    notes_url = f"{API}/cases/{cw.case_id}/notes"
    note = (await client.post(notes_url, headers=cw.lead.headers, json={"body": "Draft"})).json()
    url = f"{notes_url}/{note['id']}"
    assert (
        await client.patch(url, headers=cw.member.headers, json={"body": "x"})
    ).status_code == 403
    edited = await client.patch(url, headers=cw.lead.headers, json={"body": "Final"})
    assert edited.json()["body"] == "Final"
    assert (await client.delete(url, headers=cw.lead.headers)).status_code == 204


# ---------------------------------------------------------------- documents


async def test_document_visibility_and_signed_downloads(
    client: AsyncClient, cw: CaseWorld, storage: FakeStorage
) -> None:
    await _add_member(client, cw)
    internal = await _upload(client, cw.lead, cw.case_id, "memo.pdf", PDF, visible=False)
    shared = await _upload(client, cw.lead, cw.case_id, "evidence.png", PNG, visible=True)
    assert internal.status_code == shared.status_code == 201
    internal_id, shared_id = internal.json()["id"], shared.json()["id"]

    docs_url = f"{API}/cases/{cw.case_id}/documents"
    client_docs = [d["id"] for d in (await client.get(docs_url, headers=cw.client.headers)).json()]
    member_docs = [d["id"] for d in (await client.get(docs_url, headers=cw.member.headers)).json()]
    assert client_docs == [shared_id]
    assert member_docs == [internal_id, shared_id]

    hidden = await client.get(f"{docs_url}/{internal_id}/download", headers=cw.client.headers)
    assert hidden.status_code == 404
    ok = await client.get(f"{docs_url}/{shared_id}/download", headers=cw.client.headers)
    assert ok.status_code == 200
    assert "sig=" in ok.json()["url"]
    assert ok.json()["expires_in"] == get_settings().s3_presign_ttl_seconds
    outsider = await client.get(f"{docs_url}/{shared_id}/download", headers=cw.outsider.headers)
    assert outsider.status_code == 404

    # Storage keys never contain the user's filename.
    assert all(
        re.fullmatch(r"cases/[0-9a-f-]{36}/[0-9a-f-]{36}\.(pdf|png)", k) for k in storage.objects
    )


@pytest.mark.parametrize(
    ("filename", "data", "expected_type", "expected_name"),
    [
        ("brief.pdf", PDF, "application/pdf", "brief.pdf"),
        ("photo.PNG", PNG, "image/png", "photo.png"),
        (
            "contract.docx",
            _docx(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "contract.docx",
        ),
        # non-ASCII UTF-8 text is accepted
        ("notes.txt", "Hello \u2013 UTF-8 \u2713".encode(), "text/plain", "notes.txt"),
        ("../../etc/passwd.pdf", PDF, "application/pdf", "passwd.pdf"),
        ("scan.jpg", PDF, "application/pdf", "scan.pdf"),  # real type wins over extension
    ],
)
async def test_file_type_detected_from_content(
    client: AsyncClient,
    cw: CaseWorld,
    filename: str,
    data: bytes,
    expected_type: str,
    expected_name: str,
) -> None:
    resp = await _upload(client, cw.lead, cw.case_id, filename, data, declared="image/gif")
    assert resp.status_code == 201, resp.text
    assert resp.json()["content_type"] == expected_type
    assert resp.json()["filename"] == expected_name


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("invoice.pdf", b"MZ\x90\x00 this is a windows executable"),
        ("archive.docx", b"PK\x03\x04 not really a zip"),
        ("script.sh", b"#!/bin/sh\nrm -rf /\n"),
        ("data.txt", b"\x00\x01\x02binary"),
    ],
)
async def test_disallowed_or_spoofed_files_are_rejected(
    client: AsyncClient, cw: CaseWorld, storage: FakeStorage, filename: str, data: bytes
) -> None:
    resp = await _upload(client, cw.lead, cw.case_id, filename, data, declared="application/pdf")
    assert resp.status_code == 415
    assert storage.objects == {}


async def test_upload_size_limit(
    client: AsyncClient, cw: CaseWorld, monkeypatch: pytest.MonkeyPatch, storage: FakeStorage
) -> None:
    monkeypatch.setattr(get_settings(), "case_document_max_mb", 1)
    too_big = PDF + b"0" * (1024 * 1024)
    resp = await _upload(client, cw.lead, cw.case_id, "big.pdf", too_big)
    assert resp.status_code == 413
    assert storage.objects == {}
    empty = await _upload(client, cw.lead, cw.case_id, "empty.pdf", b"")
    assert empty.status_code == 400


async def test_client_uploads_are_always_visible_and_delete_rules(
    client: AsyncClient, cw: CaseWorld, storage: FakeStorage
) -> None:
    await _add_member(client, cw)
    mine = await _upload(client, cw.client, cw.case_id, "id.pdf", PDF, visible=False)
    assert mine.json()["visible_to_client"] is True
    lead_doc = (await _upload(client, cw.lead, cw.case_id, "x.pdf", PDF)).json()

    docs_url = f"{API}/cases/{cw.case_id}/documents"
    assert (
        await client.delete(f"{docs_url}/{lead_doc['id']}", headers=cw.client.headers)
    ).status_code == 403
    assert (
        await client.delete(f"{docs_url}/{lead_doc['id']}", headers=cw.member.headers)
    ).status_code == 403
    assert (
        await client.delete(f"{docs_url}/{mine.json()['id']}", headers=cw.lead.headers)
    ).status_code == 204
    assert len(storage.objects) == 1  # the deleted file is gone from storage too


# ---------------------------------------------------------------- hearings


async def test_hearings_schedule_and_reschedule_reminders(
    client: AsyncClient, cw: CaseWorld, jobs: FakeJobQueue
) -> None:
    await _add_member(client, cw)
    url = f"{API}/cases/{cw.case_id}/hearings"
    when = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)

    by_client = await client.post(
        url, headers=cw.client.headers, json={"scheduled_at": when.isoformat()}
    )
    assert by_client.status_code == 403
    past = await client.post(
        url,
        headers=cw.lead.headers,
        json={"scheduled_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
    )
    assert past.status_code == 400

    created = await client.post(
        url,
        headers=cw.member.headers,
        json={"scheduled_at": when.isoformat(), "court": "High Court"},
    )
    assert created.status_code == 201
    hearing_id = created.json()["id"]
    ts = int(when.timestamp())
    scheduled = {kw["_job_id"] for n, kw in jobs.jobs if n == "send_hearing_reminder"}
    assert scheduled == {
        f"hearing:{hearing_id}:reminder:24h:{ts}",
        f"hearing:{hearing_id}:reminder:1h:{ts}",
    }

    later = when + timedelta(days=1)
    moved = await client.patch(
        f"{url}/{hearing_id}", headers=cw.lead.headers, json={"scheduled_at": later.isoformat()}
    )
    assert moved.status_code == 200
    assert f"hearing:{hearing_id}:reminder:24h:{ts}" in jobs.aborted
    scheduled = {kw["_job_id"] for n, kw in jobs.jobs if n == "send_hearing_reminder"}
    assert f"hearing:{hearing_id}:reminder:1h:{int(later.timestamp())}" in scheduled

    jobs.jobs.clear()
    cancelled = await client.patch(
        f"{url}/{hearing_id}", headers=cw.lead.headers, json={"status": "cancelled"}
    )
    assert cancelled.json()["status"] == "cancelled"
    assert f"hearing:{hearing_id}:reminder:1h:{int(later.timestamp())}" in jobs.aborted
    assert not [n for n, _ in jobs.jobs if n == "send_hearing_reminder"]

    listed = await client.get(url, headers=cw.client.headers)
    assert [h["court"] for h in listed.json()] == ["High Court"]


async def test_hearing_reminder_job_emails_everyone_on_the_case(
    client: AsyncClient, cw: CaseWorld, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    await _add_member(client, cw)
    when = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    hearing = (
        await client.post(
            f"{API}/cases/{cw.case_id}/hearings",
            headers=cw.lead.headers,
            json={"scheduled_at": when.isoformat()},
        )
    ).json()
    ctx = {"session_factory": session_factory(db_session)}
    ts = int(when.timestamp())

    sent = await tasks.send_hearing_reminder(ctx, hearing_id=hearing["id"], kind="24h", start_ts=ts)
    assert sent == 3
    assert {m["to"] for m in jobs.emails()} >= {
        "owner@example.com",
        "lead@example.com",
        "member@example.com",
    }
    assert "outsider@example.com" not in {m["to"] for m in jobs.emails()}

    stale = await tasks.send_hearing_reminder(
        ctx, hearing_id=hearing["id"], kind="24h", start_ts=ts - 60
    )
    assert stale == 0
