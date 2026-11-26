"""Append-only audit log for case access and changes; admin-only listing.
Also: case emails carry the case number and a link only."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import UserRole
from app.workers import tasks
from tests.booking_support import session_factory
from tests.chat_support import ChatWorld, chat
from tests.conftest import FakeJobQueue
from tests.helpers import API, make_user

__all__ = ["chat"]

PDF = b"%PDF-1.7\n%%EOF"


async def _admin_headers(db_session: AsyncSession) -> dict[str, str]:
    return (await make_user(db_session, UserRole.ADMIN, "admin@example.com")).headers


async def _logs(
    client: AsyncClient, headers: dict[str, str], **params: str
) -> list[dict[str, object]]:
    resp = await client.get(f"{API}/admin/audit-logs", headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    items: list[dict[str, object]] = resp.json()["items"]
    return items


async def test_case_activity_is_audited(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    base = f"{API}/cases/{chat.case_id}"
    await client.get(base, headers={**chat.client.headers, "X-Request-ID": "req-view-1"})
    doc = (
        await client.post(
            f"{base}/documents", headers=chat.lead.headers, files={"file": ("a.pdf", PDF, "x/y")}
        )
    ).json()
    await client.get(f"{base}/documents/{doc['id']}/download", headers=chat.client.headers)
    await client.delete(f"{base}/documents/{doc['id']}", headers=chat.lead.headers)
    await client.delete(f"{base}/members/{chat.member.id}", headers=chat.lead.headers)
    await client.patch(base, headers=chat.lead.headers, json={"status": "on_hold"})
    await client.patch(base, headers=chat.lead.headers, json={"title": "Renamed"})  # not audited

    admin = await _admin_headers(db_session)
    logs = await _logs(client, admin, case_id=chat.case_id)
    actions = [(row["action"], row["actor_id"]) for row in reversed(logs)]
    assert actions == [
        ("case.created", str(chat.lead.id)),
        ("case.member_added", str(chat.lead.id)),
        ("case.viewed", str(chat.client.id)),
        ("document.uploaded", str(chat.lead.id)),
        ("document.downloaded", str(chat.client.id)),
        ("document.deleted", str(chat.lead.id)),
        ("case.member_removed", str(chat.lead.id)),
        ("case.status_changed", str(chat.lead.id)),
    ]
    by_action = {row["action"]: row for row in logs}
    viewed = by_action["case.viewed"]
    assert viewed["ip"] == "127.0.0.1"
    assert viewed["request_id"] == "req-view-1"
    assert viewed["actor_email"] == "owner@example.com"
    assert str(viewed["case_number"]).startswith("CASE-")
    assert by_action["document.deleted"]["entity_id"] == doc["id"]  # outlives the document
    assert by_action["case.member_removed"]["entity_id"] == str(chat.member.id)
    assert by_action["case.status_changed"]["details"] == {"from": "open", "to": "on_hold"}
    # Never privileged content.
    assert "Secret merger" not in str(logs)
    assert "Renamed" not in str(logs)


async def test_audit_listing_filters_and_is_admin_only(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    await client.get(f"{API}/cases/{chat.case_id}", headers=chat.member.headers)
    admin = await _admin_headers(db_session)
    viewed = await _logs(client, admin, action="case.viewed")
    assert [row["actor_id"] for row in viewed] == [str(chat.member.id)]
    assert await _logs(client, admin, actor_id=str(chat.client.id)) == []
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert await _logs(client, admin, since=future) == []
    for actor in (chat.lead, chat.client):
        resp = await client.get(f"{API}/admin/audit-logs", headers=actor.headers)
        assert resp.status_code == 403


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_logs SET action = 'tampered'",
        "DELETE FROM audit_logs",
        "TRUNCATE audit_logs",
    ],
)
async def test_audit_log_is_append_only(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession, statement: str
) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text(statement))


async def test_case_emails_carry_number_and_link_only(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    opened = jobs.emails_to("owner@example.com")[0]
    added = jobs.emails_to("member@example.com")[0]
    when = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    hearing = (
        await client.post(
            f"{API}/cases/{chat.case_id}/hearings",
            headers=chat.lead.headers,
            json={
                "scheduled_at": when.isoformat(),
                "court": "Family Court Room 4",
                "purpose": "Custody of minor",
            },
        )
    ).json()
    jobs.jobs.clear()
    ctx = {"session_factory": session_factory(db_session)}
    sent = await tasks.send_hearing_reminder(
        ctx, hearing_id=hearing["id"], kind="24h", start_ts=int(when.timestamp())
    )
    assert sent == 3
    reminder = jobs.emails_to("owner@example.com")[0]

    for email in (opened, added, reminder):
        text_ = email["subject"] + email["body"]
        assert "CASE-" in text_
        assert f"/cases/{chat.case_id}" in email["body"]
        for secret in ("Secret merger", "Family Court", "Custody"):
            assert secret not in text_
