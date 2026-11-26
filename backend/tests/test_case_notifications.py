"""Case updates go through notify(); the client never hears about private notes or hidden
documents, and emails carry the case number and a link only."""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from tests.chat_support import ChatWorld, chat
from tests.conftest import FakeJobQueue
from tests.helpers import API, Actor

__all__ = ["chat"]

PDF = b"%PDF-1.7\n%%EOF"


async def _types(client: AsyncClient, actor: Actor) -> list[str]:
    items = (await client.get(f"{API}/notifications", headers=actor.headers)).json()["items"]
    return [n["type"] for n in items]


async def test_status_change_notifies_everyone_but_the_actor(
    client: AsyncClient, chat: ChatWorld, jobs: FakeJobQueue
) -> None:
    jobs.jobs.clear()
    resp = await client.patch(
        f"{API}/cases/{chat.case_id}", headers=chat.lead.headers, json={"status": "on_hold"}
    )
    assert resp.status_code == 200
    for actor in (chat.client, chat.member):
        assert "case.status_changed" in await _types(client, actor)
    assert "case.status_changed" not in await _types(client, chat.lead)
    [email] = jobs.emails_to("owner@example.com")
    assert email["subject"].startswith("Update on case CASE-")
    assert f"/cases/{chat.case_id}" in email["body"]
    assert "Secret merger" not in email["subject"] + email["body"]

    # Editing other fields isn't a status change.
    await client.patch(
        f"{API}/cases/{chat.case_id}", headers=chat.lead.headers, json={"title": "x"}
    )
    assert (await _types(client, chat.client)).count("case.status_changed") == 1


async def test_notes_private_ones_stay_silent(
    client: AsyncClient, chat: ChatWorld, jobs: FakeJobQueue
) -> None:
    url = f"{API}/cases/{chat.case_id}/notes"
    await client.post(
        url, headers=chat.member.headers, json={"body": "strategy", "is_private": True}
    )
    for actor in (chat.client, chat.lead):
        assert "case.note_added" not in await _types(client, actor)

    jobs.jobs.clear()
    await client.post(url, headers=chat.client.headers, json={"body": "Here is the timeline"})
    for actor in (chat.lead, chat.member):
        assert "case.note_added" in await _types(client, actor)
    assert "case.note_added" not in await _types(client, chat.client)
    assert all("timeline" not in m["body"] for m in jobs.emails())


async def test_documents_hidden_ones_stay_silent(client: AsyncClient, chat: ChatWorld) -> None:
    url = f"{API}/cases/{chat.case_id}/documents"
    await client.post(
        url,
        headers=chat.lead.headers,
        files={"file": ("internal.pdf", PDF, "application/pdf")},
        data={"visible_to_client": "false"},
    )
    assert "case.document_added" not in await _types(client, chat.client)

    await client.post(
        url, headers=chat.lead.headers, files={"file": ("brief.pdf", PDF, "application/pdf")}
    )
    for actor in (chat.client, chat.member):
        assert (await _types(client, actor)).count("case.document_added") == 1
    assert "case.document_added" not in await _types(client, chat.lead)


async def test_hearing_added_and_changed(
    client: AsyncClient, chat: ChatWorld, jobs: FakeJobQueue
) -> None:
    when = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    jobs.jobs.clear()
    hearing = (
        await client.post(
            f"{API}/cases/{chat.case_id}/hearings",
            headers=chat.lead.headers,
            json={
                "scheduled_at": when.isoformat(),
                "court": "Family Court 4",
                "purpose": "Custody",
            },
        )
    ).json()
    assert (await _types(client, chat.client)).count("case.hearing_changed") == 1

    url = f"{API}/cases/{chat.case_id}/hearings/{hearing['id']}"
    await client.patch(
        url,
        headers=chat.member.headers,
        json={"scheduled_at": (when + timedelta(days=1)).isoformat()},
    )
    await client.patch(
        url, headers=chat.member.headers, json={"notes": "bring originals"}
    )  # silent
    await client.patch(url, headers=chat.member.headers, json={"status": "adjourned"})
    assert (await _types(client, chat.client)).count("case.hearing_changed") == 3
    assert (await _types(client, chat.lead)).count("case.hearing_changed") == 2  # not their own

    for email in jobs.emails():
        text = email["subject"] + email["body"]
        assert "Family Court" not in text
        assert "Custody" not in text
        assert "originals" not in text


async def test_case_update_emails_respect_opt_out(
    client: AsyncClient, chat: ChatWorld, jobs: FakeJobQueue
) -> None:
    await client.put(
        f"{API}/notifications/preferences",
        headers=chat.client.headers,
        json={"email": {"case.status_changed": False}},
    )
    jobs.jobs.clear()
    await client.patch(
        f"{API}/cases/{chat.case_id}", headers=chat.lead.headers, json={"status": "on_hold"}
    )
    assert jobs.emails_to("owner@example.com") == []
    assert "case.status_changed" in await _types(client, chat.client)  # in-app still on
    assert len(jobs.emails_to("member@example.com")) == 1
