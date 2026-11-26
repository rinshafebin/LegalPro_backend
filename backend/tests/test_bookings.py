import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.core.errors import ConflictError
from app.modules.advocates.models import AdvocateProfile
from app.modules.bookings import service as bookings_service
from app.modules.bookings.models import AvailabilityRule, Booking, BookingStatus
from app.modules.bookings.schemas import BookingCreateIn
from app.modules.users.models import User, UserRole
from app.workers import tasks
from tests.booking_support import (
    World,
    add_all_week_availability,
    book,
    free_slots,
    session_factory,
    set_fee,
    world,
)
from tests.conftest import FakeJobQueue
from tests.helpers import API

__all__ = ["world"]  # re-exported pytest fixture


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


# ---------------------------------------------------------------- availability


async def test_advocate_manages_weekly_availability(client: AsyncClient, world: World) -> None:
    headers = world.other_advocate.headers
    body = {
        "rules": [
            {"weekday": 0, "start_time": "09:00", "end_time": "12:00", "slot_minutes": 60},
            {"weekday": 0, "start_time": "14:00", "end_time": "16:00"},
        ]
    }
    resp = await client.put(f"{API}/advocates/me/availability", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["timezone"] == "Asia/Kolkata"
    assert len(resp.json()["rules"]) == 2

    got = await client.get(f"{API}/advocates/me/availability", headers=headers)
    assert [r["start_time"] for r in got.json()["rules"]] == ["09:00:00", "14:00:00"]


@pytest.mark.parametrize(
    ("rules", "status"),
    [
        (
            [
                {"weekday": 1, "start_time": "09:00", "end_time": "12:00"},
                {"weekday": 1, "start_time": "11:00", "end_time": "13:00"},
            ],
            400,
        ),
        ([{"weekday": 1, "start_time": "12:00", "end_time": "09:00"}], 422),
        ([{"weekday": 7, "start_time": "09:00", "end_time": "10:00"}], 422),
        ([{"weekday": 1, "start_time": "09:00", "end_time": "10:00", "slot_minutes": 5}], 422),
    ],
)
async def test_availability_validation(
    client: AsyncClient, world: World, rules: list[dict[str, object]], status: int
) -> None:
    resp = await client.put(
        f"{API}/advocates/me/availability", headers=world.advocate.headers, json={"rules": rules}
    )
    assert resp.status_code == status


async def test_only_advocates_manage_availability(client: AsyncClient, world: World) -> None:
    resp = await client.put(
        f"{API}/advocates/me/availability", headers=world.client.headers, json={"rules": []}
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------- slots


async def test_slots_follow_the_advocate_timezone(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    kolkata = await free_slots(client, world.advocate.id, days=3)
    # 09:00-17:00 IST in 30-minute slots -> UTC starts on :00/:30 between 03:30 and 11:00.
    hours = {parse(s).astimezone(UTC).strftime("%H:%M") for s in kolkata}
    assert hours <= {f"{h:02d}:{m:02d}" for h in range(3, 12) for m in (0, 30)}
    assert "11:30" not in hours  # last slot starts 16:30 IST = 11:00 UTC

    await db_session.execute(
        update(AdvocateProfile)
        .where(AdvocateProfile.user_id == world.advocate.id)
        .values(timezone="Europe/London")
    )
    await db_session.commit()
    resp = await client.get(f"{API}/advocates/{world.advocate.id}/slots", params={"days": 3})
    assert resp.json()["timezone"] == "Europe/London"
    london_hours = {parse(s["start_at"]).astimezone(UTC).hour for s in resp.json()["slots"]}
    assert london_hours <= set(range(9, 17))  # London is UTC+0 in December


async def test_slots_respect_lead_time_and_horizon(client: AsyncClient, world: World) -> None:
    slots = [parse(s) for s in await free_slots(client, world.advocate.id, days=14)]
    now = datetime.now(UTC)
    assert slots
    assert min(slots) >= now + timedelta(minutes=30)

    far = (now + timedelta(days=90)).date().isoformat()
    resp = await client.get(
        f"{API}/advocates/{world.advocate.id}/slots", params={"start_date": far, "days": 3}
    )
    assert resp.json()["slots"] == []


async def test_slots_hidden_for_unverified_advocates(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    await db_session.execute(
        update(AdvocateProfile)
        .where(AdvocateProfile.user_id == world.advocate.id)
        .values(is_verified=False)
    )
    await db_session.commit()
    resp = await client.get(f"{API}/advocates/{world.advocate.id}/slots")
    assert resp.status_code == 404


# ---------------------------------------------------------------- create


async def test_create_booking_copies_fee_and_schedules_expiry(
    client: AsyncClient, world: World, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    data = await book(client, world.client, world.advocate.id, start)

    assert data["status"] == "pending_payment"
    assert data["fee_amount"] == "1500.00"
    assert parse(data["end_at"]) - parse(data["start_at"]) == timedelta(minutes=30)
    expires = parse(data["expires_at"])
    assert timedelta(minutes=14) < expires - datetime.now(UTC) <= timedelta(minutes=15)

    expiry_jobs = [kw for name, kw in jobs.jobs if name == "expire_booking"]
    assert expiry_jobs == [
        {
            "booking_id": data["id"],
            "_defer_until": expires,
            "_job_id": f"booking:{data['id']}:expire",
        }
    ]

    # A later fee change doesn't touch the existing booking.
    await set_fee(db_session, world.advocate.id, "9999.00")
    got = await client.get(f"{API}/bookings/{data['id']}", headers=world.client.headers)
    assert got.json()["fee_amount"] == "1500.00"


async def test_booked_slot_disappears_from_free_slots(client: AsyncClient, world: World) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    await book(client, world.client, world.advocate.id, start)
    assert start not in await free_slots(client, world.advocate.id)


@pytest.mark.parametrize(
    ("who", "status", "code"),
    [("unverified_client", 403, "email_not_verified"), ("advocate", 403, "forbidden")],
)
async def test_who_can_book(
    client: AsyncClient, world: World, who: str, status: int, code: str
) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    resp = await client.post(
        f"{API}/bookings",
        headers=getattr(world, who).headers,
        json={"advocate_id": str(world.other_advocate.id), "start_at": start},
    )
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code


async def test_cannot_book_unverified_inactive_or_feeless_advocates(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    payload = {"advocate_id": str(world.advocate.id), "start_at": start}

    await set_fee(db_session, world.advocate.id, None)
    resp = await client.post(f"{API}/bookings", headers=world.client.headers, json=payload)
    assert resp.json()["error"]["code"] == "not_bookable"

    await set_fee(db_session, world.advocate.id, "1500.00")
    await db_session.execute(
        update(User).where(User.id == world.advocate.id).values(is_active=False)
    )
    await db_session.commit()
    resp = await client.post(f"{API}/bookings", headers=world.client.headers, json=payload)
    assert resp.status_code == 404


async def test_booking_time_rules(client: AsyncClient, world: World) -> None:
    start = parse((await free_slots(client, world.advocate.id))[0])
    now = datetime.now(UTC)

    async def attempt(when: datetime) -> str:
        resp = await client.post(
            f"{API}/bookings",
            headers=world.client.headers,
            json={"advocate_id": str(world.advocate.id), "start_at": when.isoformat()},
        )
        assert resp.status_code == 400
        code: str = resp.json()["error"]["code"]
        return code

    assert await attempt(now - timedelta(hours=1)) == "too_soon"
    assert await attempt(now + timedelta(days=61)) == "beyond_horizon"
    assert await attempt(start + timedelta(minutes=10)) == "not_a_slot"

    naive = await client.post(
        f"{API}/bookings",
        headers=world.client.headers,
        json={"advocate_id": str(world.advocate.id), "start_at": "2030-01-01T10:00:00"},
    )
    assert naive.status_code == 422  # timestamps must carry an offset


async def test_same_slot_cannot_be_booked_twice(client: AsyncClient, world: World) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    await book(client, world.client, world.advocate.id, start)
    resp = await client.post(
        f"{API}/bookings",
        headers=world.other_client.headers,
        json={"advocate_id": str(world.advocate.id), "start_at": start},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "slot_unavailable"


async def test_client_cannot_overlap_own_bookings(client: AsyncClient, world: World) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    await book(client, world.client, world.advocate.id, start)
    resp = await client.post(
        f"{API}/bookings",
        headers=world.client.headers,
        json={"advocate_id": str(world.other_advocate.id), "start_at": start},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "client_overlap"


async def test_exclusion_constraint_blocks_partial_overlap(
    db_session: AsyncSession, world: World
) -> None:
    """The DB, not just the app, refuses overlapping ranges (not only identical starts)."""
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)

    def booking(client_id: uuid.UUID, offset_min: int) -> Booking:
        s = start + timedelta(minutes=offset_min)
        return Booking(
            client_id=client_id,
            advocate_id=world.advocate.id,
            start_at=s,
            end_at=s + timedelta(minutes=60),
            status=BookingStatus.CONFIRMED,
            fee_amount=Decimal("1"),
            currency="INR",
        )

    db_session.add(booking(world.client.id, 0))
    await db_session.commit()
    db_session.add(booking(world.other_client.id, 30))
    with pytest.raises(IntegrityError, match="ex_bookings_advocate_overlap"):
        await db_session.commit()
    await db_session.rollback()

    # Inactive bookings don't hold the slot.
    await db_session.execute(update(Booking).values(status=BookingStatus.CANCELLED))
    db_session.add(booking(world.other_client.id, 30))
    await db_session.commit()


async def test_concurrent_double_booking_only_one_wins() -> None:
    """Real concurrency: separate connections race to book the same slot.

    Uses committed data (not the per-test rollback) so the transactions truly compete.
    """
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as setup:
        advocate = User(
            email=f"race-adv-{tag}@example.com", role=UserRole.ADVOCATE, email_verified=True
        )
        advocate.advocate_profile = AdvocateProfile(
            full_name="Race Advocate",
            bar_council_id=f"RACE/{tag}",
            is_verified=True,
            consultation_fee=Decimal("500.00"),
        )
        clients = [
            User(email=f"race-c{i}-{tag}@example.com", role=UserRole.CLIENT, email_verified=True)
            for i in range(5)
        ]
        setup.add_all([advocate, *clients])
        await setup.commit()
        await add_all_week_availability(setup, advocate.id)
        advocate_id, client_ids = advocate.id, [c.id for c in clients]

    try:
        async with SessionLocal() as s:
            _, slots = await bookings_service.list_free_slots(s, advocate_id, None, 3)
        start_at = slots[0].start

        async def attempt(client_id: uuid.UUID) -> str:
            async with SessionLocal() as session:
                user = await session.get(User, client_id)
                assert user is not None
                try:
                    await bookings_service.create_booking(
                        session, user, BookingCreateIn(advocate_id=advocate_id, start_at=start_at)
                    )
                except ConflictError as exc:
                    return exc.code
                return "booked"

        results = await asyncio.gather(*(attempt(c) for c in client_ids))
        assert sorted(results) == ["booked"] + ["slot_unavailable"] * 4
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(delete(Booking).where(Booking.advocate_id == advocate_id))
            await cleanup.execute(
                delete(AvailabilityRule).where(AvailabilityRule.advocate_id == advocate_id)
            )
            await cleanup.execute(delete(User).where(User.id.in_([advocate_id, *client_ids])))
            await cleanup.commit()


# ---------------------------------------------------------------- expiry


async def test_unpaid_booking_expires_and_frees_the_slot(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    data = await book(client, world.client, world.advocate.id, start)
    ctx = {"session_factory": session_factory(db_session)}

    # Too early: the job is a no-op.
    assert await tasks.expire_booking(ctx, booking_id=data["id"]) is False

    await db_session.execute(
        update(Booking).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()
    assert await tasks.expire_booking(ctx, booking_id=data["id"]) is True

    got = await client.get(f"{API}/bookings/{data['id']}", headers=world.client.headers)
    assert got.json()["status"] == "expired"
    assert start in await free_slots(client, world.advocate.id)
    await book(client, world.other_client, world.advocate.id, start)


async def test_expiry_sweep_catches_lost_jobs(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    slots = await free_slots(client, world.advocate.id)
    await book(client, world.client, world.advocate.id, slots[0])
    await book(client, world.other_client, world.advocate.id, slots[1])
    await db_session.execute(
        update(Booking).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db_session.commit()

    ctx = {"session_factory": session_factory(db_session)}
    assert await tasks.expire_stale_bookings(ctx) == 2
    assert await tasks.expire_stale_bookings(ctx) == 0


# ---------------------------------------------------------------- free consultations


async def test_free_consultation_is_confirmed_immediately(
    client: AsyncClient, world: World, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    await set_fee(db_session, world.advocate.id, "0")
    start = (await free_slots(client, world.advocate.id, days=5))[-1]
    data = await book(client, world.client, world.advocate.id, start)

    assert data["status"] == "confirmed"
    assert data["expires_at"] is None
    reminder_ids = [kw["_job_id"] for name, kw in jobs.jobs if name == "send_booking_reminder"]
    ts = int(parse(start).timestamp())
    assert f"booking:{data['id']}:reminder:1h:{ts}" in reminder_ids
    assert jobs.emails_to("c1@example.com")[-1]["subject"] == "Consultation confirmed"
    assert jobs.emails_to("adv@example.com")[-1]["subject"] == "Consultation confirmed"


# ---------------------------------------------------------------- visibility & listing


async def test_booking_visibility(client: AsyncClient, world: World) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    data = await book(client, world.client, world.advocate.id, start)
    url = f"{API}/bookings/{data['id']}"

    assert (await client.get(url, headers=world.client.headers)).status_code == 200
    assert (await client.get(url, headers=world.advocate.headers)).status_code == 200
    for outsider in (world.other_client, world.other_advocate):
        assert (await client.get(url, headers=outsider.headers)).status_code == 404

    mine = await client.get(f"{API}/bookings", headers=world.advocate.headers)
    assert [b["id"] for b in mine.json()["items"]] == [data["id"]]
    theirs = await client.get(f"{API}/bookings", headers=world.other_client.headers)
    assert theirs.json()["items"] == []
    assert data["client"]["full_name"] == "Asha"
    assert data["advocate"]["full_name"] == "Adv. Meera"


async def test_cancel_pending_booking(
    client: AsyncClient, world: World, jobs: FakeJobQueue
) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    data = await book(client, world.client, world.advocate.id, start)
    url = f"{API}/bookings/{data['id']}/cancel"

    other = await client.post(url, headers=world.other_client.headers, json={})
    assert other.status_code == 404
    resp = await client.post(url, headers=world.client.headers, json={"reason": "Changed plans"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert f"booking:{data['id']}:expire" in jobs.aborted
    assert start in await free_slots(client, world.advocate.id)

    again = await client.post(url, headers=world.client.headers, json={})
    assert again.status_code == 409


async def test_availability_change_keeps_existing_bookings(
    client: AsyncClient, world: World
) -> None:
    start = (await free_slots(client, world.advocate.id))[0]
    data = await book(client, world.client, world.advocate.id, start)
    resp = await client.put(
        f"{API}/advocates/me/availability", headers=world.advocate.headers, json={"rules": []}
    )
    assert resp.status_code == 200
    got = await client.get(f"{API}/bookings/{data['id']}", headers=world.client.headers)
    assert got.json()["status"] == "pending_payment"
    assert await free_slots(client, world.advocate.id) == []
