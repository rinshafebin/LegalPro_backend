"""Chat over REST: rooms, access, history, read receipts, limits, attachments."""

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.bookings.models import BookingStatus
from app.modules.chat.models import ChatMessage, ChatRoom
from tests.booking_support import World, book, free_slots, set_fee, webhook_request, world
from tests.chat_support import ChatWorld, chat, send
from tests.conftest import FakeJobQueue, FakeStorage
from tests.helpers import API

__all__ = ["chat", "world"]

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


async def _rooms(client: AsyncClient, headers: dict[str, str]) -> list[dict[str, object]]:
    resp = await client.get(f"{API}/chat/rooms", headers=headers)
    assert resp.status_code == 200, resp.text
    items: list[dict[str, object]] = resp.json()["items"]
    return items


# ---------------------------------------------------------------- rooms


async def test_case_gets_a_room_for_everyone_on_it(client: AsyncClient, chat: ChatWorld) -> None:
    for actor in (chat.client, chat.lead, chat.member):
        rooms = await _rooms(client, actor.headers)
        assert [r["id"] for r in rooms] == [chat.room_id]
    assert rooms[0]["kind"] == "case"
    assert str(rooms[0]["case_number"]).startswith("CASE-")
    for outsider in (chat.outsider, chat.other_client):
        assert await _rooms(client, outsider.headers) == []
        resp = await client.get(f"{API}/chat/rooms/{chat.room_id}", headers=outsider.headers)
        assert resp.status_code == 404


async def test_room_detail_lists_participants(client: AsyncClient, chat: ChatWorld) -> None:
    detail = (
        await client.get(f"{API}/chat/rooms/{chat.room_id}", headers=chat.client.headers)
    ).json()
    assert {p["id"] for p in detail["participants"]} == {
        str(chat.client.id),
        str(chat.lead.id),
        str(chat.member.id),
    }
    assert detail["writable"] is True


async def test_room_is_created_when_a_free_booking_is_confirmed(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    await set_fee(db_session, world.advocate.id, "0")
    start = (await free_slots(client, world.advocate.id))[0]
    booking = await book(client, world.client, world.advocate.id, start)
    assert booking["status"] == "confirmed"
    rooms = await _rooms(client, world.client.headers)
    assert [r["booking_id"] for r in rooms] == [booking["id"]]
    assert rooms[0]["title"] == "Consultation with Adv. Meera"
    assert await _rooms(client, world.other_client.headers) == []


async def test_room_is_created_only_once_payment_confirms_the_booking(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    booking = await book(client, world.client, world.advocate.id, start)
    assert booking["status"] == "pending_payment"
    assert await db_session.scalar(select(func.count()).select_from(ChatRoom)) == 0

    order = (
        await client.post(
            f"{API}/payments/orders",
            headers=world.client.headers,
            json={"booking_id": booking["id"]},
        )
    ).json()
    body, headers = webhook_request(order["order_id"], order["amount"])
    await client.post(f"{API}/payments/webhook", content=body, headers=headers)
    body, headers = webhook_request(order["order_id"], order["amount"])  # redelivery
    await client.post(f"{API}/payments/webhook", content=body, headers=headers)
    assert await db_session.scalar(select(func.count()).select_from(ChatRoom)) == 1


# ---------------------------------------------------------------- messages


async def test_sender_is_always_the_authenticated_user(
    client: AsyncClient, chat: ChatWorld
) -> None:
    message = await send(client, chat.client, chat.room_id, "hi", sender_id=str(chat.lead.id))
    assert message["sender_id"] == str(chat.client.id)


@pytest.mark.parametrize("who", ["outsider", "other_client"])
async def test_non_participants_cant_read_or_write(
    client: AsyncClient, chat: ChatWorld, who: str
) -> None:
    actor = getattr(chat, who)
    url = f"{API}/chat/rooms/{chat.room_id}/messages"
    assert (await client.get(url, headers=actor.headers)).status_code == 404
    assert (await client.post(url, headers=actor.headers, json={"body": "x"})).status_code == 404


@pytest.mark.parametrize("body", ["", "   ", "x" * 4001])
async def test_message_length_limits(client: AsyncClient, chat: ChatWorld, body: str) -> None:
    resp = await client.post(
        f"{API}/chat/rooms/{chat.room_id}/messages",
        headers=chat.client.headers,
        json={"body": body},
    )
    assert resp.status_code == 422
    assert (await send(client, chat.client, chat.room_id, "x" * 4000))["body"] == "x" * 4000


async def test_per_user_rate_limit(client: AsyncClient, chat: ChatWorld) -> None:
    for i in range(get_settings().chat_rate_limit_messages):
        await send(client, chat.client, chat.room_id, f"m{i}")
    resp = await client.post(
        f"{API}/chat/rooms/{chat.room_id}/messages", headers=chat.client.headers, json={"body": "!"}
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    # Someone else in the room is unaffected.
    await send(client, chat.lead, chat.room_id, "still fine")


async def test_resending_a_client_msg_id_does_not_duplicate(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    cmid = str(uuid.uuid4())
    url = f"{API}/chat/rooms/{chat.room_id}/messages"
    first = await client.post(
        url, headers=chat.client.headers, json={"body": "a", "client_msg_id": cmid}
    )
    again = await client.post(
        url, headers=chat.client.headers, json={"body": "a", "client_msg_id": cmid}
    )
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert await db_session.scalar(select(func.count()).select_from(ChatMessage)) == 1


async def test_cursor_pagination(client: AsyncClient, chat: ChatWorld) -> None:
    sent = [await send(client, chat.lead, chat.room_id, f"m{i}") for i in range(7)]
    url = f"{API}/chat/rooms/{chat.room_id}/messages"

    page1 = (await client.get(url, headers=chat.client.headers, params={"limit": 3})).json()
    assert [m["body"] for m in page1["items"]] == ["m6", "m5", "m4"]
    page2 = (
        await client.get(
            url, headers=chat.client.headers, params={"limit": 3, "before": page1["next_cursor"]}
        )
    ).json()
    assert [m["body"] for m in page2["items"]] == ["m3", "m2", "m1"]
    page3 = (
        await client.get(
            url, headers=chat.client.headers, params={"limit": 3, "before": page2["next_cursor"]}
        )
    ).json()
    assert [m["body"] for m in page3["items"]] == ["m0"]
    assert page3["next_cursor"] is None

    caught_up = (
        await client.get(url, headers=chat.client.headers, params={"after": sent[4]["seq"]})
    ).json()
    assert [m["body"] for m in caught_up["items"]] == ["m5", "m6"]


async def test_read_receipts_and_unread_counts(client: AsyncClient, chat: ChatWorld) -> None:
    messages = [await send(client, chat.lead, chat.room_id, f"m{i}") for i in range(3)]
    await send(client, chat.client, chat.room_id, "mine")  # own messages never count as unread

    async def unread() -> int:
        rooms = await _rooms(client, chat.client.headers)
        total = (await client.get(f"{API}/chat/unread", headers=chat.client.headers)).json()
        assert total["unread"] == rooms[0]["unread_count"]
        return int(str(rooms[0]["unread_count"]))

    # Sending a message marks everything before it as read for the sender.
    assert await unread() == 0
    await send(client, chat.lead, chat.room_id, "later")
    assert await unread() == 1
    # Replying also reads: the lead's "later" moved their marker past the client's "mine".
    assert (await _rooms(client, chat.lead.headers))[0]["unread_count"] == 0

    url = f"{API}/chat/rooms/{chat.room_id}/read"
    latest = (
        await client.get(f"{API}/chat/rooms/{chat.room_id}/messages", headers=chat.client.headers)
    ).json()["items"][0]
    read = await client.post(url, headers=chat.client.headers, json={"seq": latest["seq"]})
    assert read.json()["last_read_seq"] == latest["seq"]
    assert await unread() == 0

    # Never moves backwards, and can't run ahead of the newest message.
    back = await client.post(url, headers=chat.client.headers, json={"seq": messages[0]["seq"]})
    assert back.json()["last_read_seq"] == latest["seq"]
    ahead = await client.post(url, headers=chat.client.headers, json={"seq": 10**12})
    assert ahead.json()["last_read_seq"] == latest["seq"]

    detail = (
        await client.get(f"{API}/chat/rooms/{chat.room_id}", headers=chat.lead.headers)
    ).json()
    receipts = {p["id"]: p["last_read_seq"] for p in detail["participants"]}
    assert receipts[str(chat.client.id)] == latest["seq"]


# ---------------------------------------------------------------- losing access


async def test_removed_member_loses_the_room_at_once(client: AsyncClient, chat: ChatWorld) -> None:
    await send(client, chat.member, chat.room_id, "before")
    resp = await client.delete(
        f"{API}/cases/{chat.case_id}/members/{chat.member.id}", headers=chat.lead.headers
    )
    assert resp.status_code == 204
    url = f"{API}/chat/rooms/{chat.room_id}/messages"
    assert (await client.get(url, headers=chat.member.headers)).status_code == 404
    assert (
        await client.post(url, headers=chat.member.headers, json={"body": "x"})
    ).status_code == 404
    assert await _rooms(client, chat.member.headers) == []


async def test_closed_case_room_is_read_only(client: AsyncClient, chat: ChatWorld) -> None:
    await send(client, chat.client, chat.room_id, "before closing")
    closed = await client.patch(
        f"{API}/cases/{chat.case_id}", headers=chat.lead.headers, json={"status": "closed"}
    )
    assert closed.status_code == 200
    url = f"{API}/chat/rooms/{chat.room_id}/messages"
    assert len((await client.get(url, headers=chat.client.headers)).json()["items"]) == 1
    resp = await client.post(url, headers=chat.client.headers, json={"body": "after"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "case_closed"
    detail = (
        await client.get(f"{API}/chat/rooms/{chat.room_id}", headers=chat.client.headers)
    ).json()
    assert detail["writable"] is False


async def test_pending_or_cancelled_booking_has_no_usable_room(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    from app.modules.chat.service import ensure_booking_room
    from tests.chat_support import make_booking

    booking_id = await make_booking(
        db_session, chat.client.id, chat.lead.id, BookingStatus.CANCELLED
    )
    await ensure_booking_room(db_session, booking_id)
    rooms = await _rooms(client, chat.client.headers)
    assert str(booking_id) not in {r["booking_id"] for r in rooms}


# ---------------------------------------------------------------- attachments


async def test_attachment_upload_and_download(
    client: AsyncClient, chat: ChatWorld, storage: FakeStorage
) -> None:
    resp = await client.post(
        f"{API}/chat/rooms/{chat.room_id}/attachments",
        headers=chat.client.headers,
        files={"file": ("../../scan.pdf", PDF, "image/png")},
        data={"body": "Signed copy"},
    )
    assert resp.status_code == 201, resp.text
    message = resp.json()
    assert message["attachment"] == {
        "filename": "scan.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(PDF),
    }
    assert message["body"] == "Signed copy"
    [key] = storage.objects
    assert key.startswith(f"chat/{chat.room_id}/")
    assert key.endswith(".pdf")

    url = f"{API}/chat/rooms/{chat.room_id}/messages/{message['id']}/attachment"
    download = await client.get(url, headers=chat.member.headers)
    assert download.status_code == 200
    assert "sig=" in download.json()["url"]
    assert (await client.get(url, headers=chat.outsider.headers)).status_code == 404


async def test_attachment_uses_case_document_validation(
    client: AsyncClient, chat: ChatWorld, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"{API}/chat/rooms/{chat.room_id}/attachments"
    spoof = await client.post(
        url,
        headers=chat.client.headers,
        files={"file": ("invoice.pdf", b"MZ\x90\x00binary", "application/pdf")},
    )
    assert spoof.status_code == 415

    monkeypatch.setattr(get_settings(), "chat_attachment_max_mb", 1)
    big = await client.post(
        url,
        headers=chat.client.headers,
        files={"file": ("big.pdf", b"%PDF-" + b"0" * (1024 * 1024 + 1), "application/pdf")},
    )
    assert big.status_code == 413
    assert storage.objects == {}


# ---------------------------------------------------------------- notifications


async def test_new_message_notifies_others_once_without_the_text(
    client: AsyncClient, chat: ChatWorld, jobs: FakeJobQueue
) -> None:
    jobs.jobs.clear()
    await send(client, chat.lead, chat.room_id, "The settlement figure is 4.2 crore")
    await send(client, chat.lead, chat.room_id, "Second message")  # throttled

    for actor in (chat.client, chat.member):
        notes = (await client.get(f"{API}/notifications", headers=actor.headers)).json()["items"]
        chat_notes = [n for n in notes if n["type"] == "chat.message"]
        assert len(chat_notes) == 1
        assert "settlement" not in str(chat_notes[0])
    lead_notes = (await client.get(f"{API}/notifications", headers=chat.lead.headers)).json()
    assert not [n for n in lead_notes["items"] if n["type"] == "chat.message"]

    emails = jobs.emails_to("owner@example.com")
    assert len(emails) == 1
    assert "settlement" not in emails[0]["body"]
    assert "4.2" not in emails[0]["body"]
    assert "Secret merger" not in emails[0]["body"] + emails[0]["subject"]
    assert "CASE-" in emails[0]["subject"]
    assert f"/chat/{chat.room_id}" in emails[0]["body"]


# ---------------------------------------------------------------- edit / delete


async def _admin(db_session: AsyncSession) -> dict[str, str]:
    from app.modules.users.models import UserRole
    from tests.helpers import make_user

    return (await make_user(db_session, UserRole.ADMIN, "admin@example.com")).headers


async def test_sender_can_edit_within_the_window(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    from sqlalchemy import text, update

    message = await send(client, chat.client, chat.room_id, "Meet at 10")
    url = f"{API}/chat/rooms/{chat.room_id}/messages/{message['id']}"
    edited = await client.patch(url, headers=chat.client.headers, json={"body": "Meet at 11"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["body"] == "Meet at 11"
    assert edited.json()["edited"] is True
    listed = (
        await client.get(f"{API}/chat/rooms/{chat.room_id}/messages", headers=chat.lead.headers)
    ).json()
    assert listed["items"][0]["edited"] is True

    # The original text is kept in the audit log, but the admin API doesn't show it.
    admin = await _admin(db_session)
    [log] = (
        await client.get(
            f"{API}/admin/audit-logs", headers=admin, params={"action": "chat.message_edited"}
        )
    ).json()["items"]
    assert log["entity_id"] == message["id"]
    assert log["details"]["previous_body"] == "[retained, not shown]"
    stored = await db_session.scalar(
        text(
            "SELECT details->>'previous_body' FROM audit_logs WHERE action = 'chat.message_edited'"
        )
    )
    assert stored == "Meet at 10"

    # Others can't edit; the window closes after 15 minutes.
    assert (
        await client.patch(url, headers=chat.lead.headers, json={"body": "x"})
    ).status_code == 403
    assert (
        await client.patch(url, headers=chat.outsider.headers, json={"body": "x"})
    ).status_code == 404
    await db_session.execute(
        update(ChatMessage).values(created_at=ChatMessage.created_at - timedelta(minutes=16))
    )
    await db_session.commit()
    late = await client.patch(url, headers=chat.client.headers, json={"body": "Meet at 12"})
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "edit_window_closed"


async def test_soft_delete_leaves_a_placeholder(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession, storage: FakeStorage
) -> None:
    from sqlalchemy import text

    resp = await client.post(
        f"{API}/chat/rooms/{chat.room_id}/attachments",
        headers=chat.lead.headers,
        files={"file": ("draft.pdf", PDF, "application/pdf")},
        data={"body": "draft attached"},
    )
    message = resp.json()
    url = f"{API}/chat/rooms/{chat.room_id}/messages/{message['id']}"
    assert (await client.delete(url, headers=chat.client.headers)).status_code == 403

    deleted = await client.delete(url, headers=chat.lead.headers)
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["deleted"] is True
    assert body["body"] is None
    assert body["attachment"] is None
    listed = (
        await client.get(f"{API}/chat/rooms/{chat.room_id}/messages", headers=chat.client.headers)
    ).json()
    assert listed["items"][0]["deleted"] is True
    assert listed["items"][0]["body"] is None
    download = await client.get(f"{url}/attachment", headers=chat.client.headers)
    assert download.status_code == 404
    assert len(storage.objects) == 1  # soft delete: the object is retained
    assert (await client.get(f"{API}/chat/unread", headers=chat.client.headers)).json()[
        "unread"
    ] == 0

    again = await client.delete(url, headers=chat.lead.headers)
    assert again.status_code == 200  # idempotent
    edit = await client.patch(url, headers=chat.lead.headers, json={"body": "undo"})
    assert edit.json()["error"]["code"] == "message_deleted"
    stored = await db_session.scalar(
        text("SELECT details FROM audit_logs WHERE action = 'chat.message_deleted'")
    )
    assert stored["previous_body"] == "draft attached"
    assert stored["attachment"]["filename"] == "draft.pdf"


async def test_removed_member_cant_change_old_messages(
    client: AsyncClient, chat: ChatWorld
) -> None:
    message = await send(client, chat.member, chat.room_id, "mine")
    await client.delete(
        f"{API}/cases/{chat.case_id}/members/{chat.member.id}", headers=chat.lead.headers
    )
    url = f"{API}/chat/rooms/{chat.room_id}/messages/{message['id']}"
    assert (
        await client.patch(url, headers=chat.member.headers, json={"body": "x"})
    ).status_code == 404
    assert (await client.delete(url, headers=chat.member.headers)).status_code == 404
