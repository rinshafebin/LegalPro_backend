"""notify(): stored in-app, pushed, emailed through the sealed queue; per-type email opt-out."""

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.models import Notification, NotificationType
from app.modules.notifications.service import notify
from app.modules.users.models import User, UserRole
from tests.booking_support import World, book, free_slots, set_fee, world
from tests.conftest import FakeJobQueue
from tests.helpers import API, make_user

__all__ = ["world"]


async def _user(db_session: AsyncSession, email: str, role: UserRole = UserRole.CLIENT) -> User:
    actor = await make_user(db_session, role, email)
    user = await db_session.get(User, actor.id)
    assert user is not None
    return user


async def test_notify_stores_and_emails_sealed(
    db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    user = await _user(db_session, "n@example.com")
    note = await notify(
        db_session,
        user,
        NotificationType.BOOKING_CONFIRMED,
        title="Consultation confirmed",
        data={"booking_id": "b1"},
        email=("Consultation confirmed", "See you then."),
    )
    assert note is not None
    assert note.read_at is None
    [(_name, kwargs)] = [(n, kw) for n, kw in jobs.jobs if n == "send_email"]
    assert set(kwargs) == {"sealed", "_defer_until", "_job_id"}  # nothing readable in the job
    assert "n@example.com" not in kwargs["sealed"]
    assert jobs.emails_to("n@example.com")[0]["subject"] == "Consultation confirmed"


async def test_email_opt_out_is_per_type_and_keeps_in_app(
    client: AsyncClient, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    actor = await make_user(db_session, UserRole.CLIENT, "pref@example.com")
    prefs = (await client.get(f"{API}/notifications/preferences", headers=actor.headers)).json()
    assert {p["type"] for p in prefs} == {t.value for t in NotificationType} - {"refund.attention"}
    assert all(p["email_enabled"] for p in prefs)

    resp = await client.put(
        f"{API}/notifications/preferences",
        headers=actor.headers,
        json={"email": {"booking.reminder": False, "refund.attention": False}},
    )
    assert resp.status_code == 200
    changed = {p["type"]: p["email_enabled"] for p in resp.json()}
    assert changed["booking.reminder"] is False
    assert changed["booking.confirmed"] is True

    user = await db_session.get(User, actor.id)
    assert user is not None
    for type_ in (NotificationType.BOOKING_REMINDER, NotificationType.BOOKING_CONFIRMED):
        await notify(db_session, user, type_, title=type_.value, email=(type_.value, "body"))
    assert [m["subject"] for m in jobs.emails_to("pref@example.com")] == ["booking.confirmed"]
    listed = (await client.get(f"{API}/notifications", headers=actor.headers)).json()
    assert {n["type"] for n in listed["items"]} == {"booking.reminder", "booking.confirmed"}


async def test_list_unread_and_mark_read(client: AsyncClient, db_session: AsyncSession) -> None:
    actor = await make_user(db_session, UserRole.CLIENT, "inbox@example.com")
    other = await make_user(db_session, UserRole.CLIENT, "nosy@example.com")
    user = await db_session.get(User, actor.id)
    assert user is not None
    for i in range(3):
        await notify(db_session, user, NotificationType.CASE_OPENED, title=f"n{i}")

    async def count() -> int:
        resp = await client.get(f"{API}/notifications/unread-count", headers=actor.headers)
        return int(resp.json()["unread"])

    assert await count() == 3
    first = (await client.get(f"{API}/notifications", headers=actor.headers)).json()["items"][0]
    assert first["title"] == "n2"  # newest first

    # Another user can't read or mark someone else's notification.
    url = f"{API}/notifications/{first['id']}/read"
    assert (await client.post(url, headers=other.headers)).status_code == 404
    assert (await client.post(url, headers=actor.headers)).json()["read_at"] is not None
    assert await count() == 2
    unread = (
        await client.get(f"{API}/notifications", headers=actor.headers, params={"unread": True})
    ).json()
    assert len(unread["items"]) == 2

    await client.post(f"{API}/notifications/read-all", headers=actor.headers)
    assert await count() == 0


async def test_inactive_users_get_nothing(db_session: AsyncSession, jobs: FakeJobQueue) -> None:
    user = await _user(db_session, "gone@example.com")
    await db_session.execute(update(User).where(User.id == user.id).values(is_active=False))
    await db_session.commit()
    await db_session.refresh(user)
    assert await notify(db_session, user, NotificationType.CASE_OPENED, title="x") is None
    assert await db_session.scalar(select(Notification.id)) is None
    assert jobs.emails() == []


async def test_booking_confirmation_notifies_both_sides(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    await set_fee(db_session, world.advocate.id, "0")
    start = (await free_slots(client, world.advocate.id))[0]
    booking = await book(client, world.client, world.advocate.id, start)
    for actor in (world.client, world.advocate):
        items = (await client.get(f"{API}/notifications", headers=actor.headers)).json()["items"]
        assert [(n["type"], n["data"]["booking_id"]) for n in items] == [
            ("booking.confirmed", booking["id"])
        ]
