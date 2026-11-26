"""Account deletion (14-day grace, blockers, anonymization) and personal data export."""

from datetime import UTC, datetime, timedelta

import pyotp
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.security import encrypt_str
from app.modules.bookings.models import Booking, BookingStatus
from app.modules.cases.models import Case
from app.modules.chat.models import ChatMessage
from app.modules.clients.models import ClientProfile
from app.modules.notifications.models import Notification
from app.modules.users.models import User, UserRole
from app.workers import tasks
from tests.booking_support import session_factory
from tests.chat_support import ChatWorld, chat, make_booking, send
from tests.conftest import FakeJobQueue, FakeStorage
from tests.helpers import API, PASSWORD, bearer, make_user, register_client, totp_code

__all__ = ["chat"]
URL = f"{API}/users/me/deletion"


async def _user(db_session: AsyncSession, email: str) -> User:
    db_session.expire_all()
    user = await db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    return user


async def test_request_needs_password_then_deactivates(
    client: AsyncClient, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    reg = await register_client(client, "leaver@example.com")
    headers = bearer(reg["tokens"]["access_token"])
    wrong = await client.post(URL, headers=headers, json={"password": "nope-nope-nope"})
    assert wrong.status_code == 401

    resp = await client.post(URL, headers=headers, json={"password": PASSWORD})
    assert resp.status_code == 202, resp.text
    scheduled = datetime.fromisoformat(resp.json()["scheduled_for"])
    assert timedelta(days=13, hours=23) < scheduled - datetime.now(UTC) <= timedelta(days=14)

    user = await _user(db_session, "leaver@example.com")
    assert user.is_active is False
    assert (await client.get(f"{API}/users/me", headers=headers)).status_code == 401
    [kwargs] = [kw for n, kw in jobs.jobs if n == "delete_account"]
    assert kwargs["_job_id"] == f"user:{user.id}:delete:{int(scheduled.timestamp())}"
    assert kwargs["_defer_until"] == scheduled
    assert jobs.emails_to("leaver@example.com")[-1]["subject"] == "Your account will be deleted"


async def test_logging_in_during_grace_cancels(
    client: AsyncClient, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    reg = await register_client(client, "undo@example.com")
    await client.post(
        URL, headers=bearer(reg["tokens"]["access_token"]), json={"password": PASSWORD}
    )
    login = await client.post(
        f"{API}/auth/login", json={"email": "undo@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["tokens"] is not None
    user = await _user(db_session, "undo@example.com")
    assert user.is_active is True
    assert user.deletion_scheduled_for is None
    assert any(j.startswith(f"user:{user.id}:delete:") for j in jobs.aborted)
    assert jobs.emails_to("undo@example.com")[-1]["subject"] == "Account deletion cancelled"


async def test_mfa_users_need_a_code(client: AsyncClient, db_session: AsyncSession) -> None:
    reg = await register_client(client, "mfa@example.com")
    secret = pyotp.random_base32()
    await db_session.execute(
        update(User)
        .where(User.email == "mfa@example.com")
        .values(mfa_enabled=True, mfa_secret_encrypted=encrypt_str(secret))
    )
    await db_session.commit()
    headers = bearer(reg["tokens"]["access_token"])
    missing = await client.post(URL, headers=headers, json={"password": PASSWORD})
    assert missing.json()["error"]["code"] == "mfa_code_required"
    ok = await client.post(
        URL, headers=headers, json={"password": PASSWORD, "code": totp_code(secret)}
    )
    assert ok.status_code == 202


async def test_passwordless_accounts_need_a_recent_login(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    actor = await make_user(db_session, UserRole.CLIENT, "google@example.com")
    user = await _user(db_session, "google@example.com")
    issued = datetime.now(UTC) - timedelta(minutes=30)  # logged in half an hour ago
    stale = security._encode(
        {
            "sub": str(user.id),
            "type": "access",
            "jti": "old",
            "iat": issued,
            "exp": issued + timedelta(hours=1),
            "ver": user.token_version,
            "role": "client",
        }
    )
    resp = await client.post(URL, headers=bearer(stale), json={})
    assert resp.json()["error"]["code"] == "recent_login_required"
    assert (await client.post(URL, headers=actor.headers, json={})).status_code == 202


async def test_admins_cant_self_delete(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    resp = await client.post(URL, headers=admin.headers, json={})
    assert resp.json()["error"]["code"] == "admin_cannot_self_delete"


async def test_blocked_while_obligations_remain(client: AsyncClient, chat: ChatWorld) -> None:
    # The lead has an open case and the client an upcoming confirmed booking (fixture's).
    lead = await client.post(URL, headers=chat.lead.headers, json={})
    assert lead.status_code == 409
    assert lead.json()["error"]["code"] == "deletion_blocked"
    assert lead.json()["error"]["details"]["open_cases_you_lead"] == 1

    await client.patch(
        f"{API}/cases/{chat.case_id}", headers=chat.lead.headers, json={"status": "closed"}
    )
    assert (await client.post(URL, headers=chat.lead.headers, json={})).status_code == 202
    # A member (not lead) of an open case isn't blocked.
    assert (await client.post(URL, headers=chat.member.headers, json={})).status_code == 202


async def test_upcoming_booking_blocks(
    client: AsyncClient, db_session: AsyncSession, chat: ChatWorld
) -> None:
    await make_booking(
        db_session, chat.other_client.id, chat.outsider.id, BookingStatus.CONFIRMED, days=4
    )
    resp = await client.post(URL, headers=chat.other_client.headers, json={})
    assert resp.json()["error"]["details"] == {"upcoming_bookings": 1}


async def test_anonymization_wipes_personal_data_and_keeps_records(
    client: AsyncClient,
    chat: ChatWorld,
    db_session: AsyncSession,
    jobs: FakeJobQueue,
    storage: FakeStorage,
) -> None:
    # The client has a case, chat history, a notification and a profile image.
    await send(client, chat.client, chat.room_id, "My statement for the record")
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    await client.put(
        f"{API}/users/me/avatar",
        headers=chat.client.headers,
        files={"file": ("a.png", buf.getvalue(), "image/png")},
    )
    await db_session.execute(update(Booking).values(status=BookingStatus.COMPLETED))
    await db_session.commit()

    resp = await client.post(URL, headers=chat.client.headers, json={})
    assert resp.status_code == 202, resp.text
    ctx = {"session_factory": session_factory(db_session)}
    user = await _user(db_session, "owner@example.com")
    ts = int(user.deletion_scheduled_for.timestamp())  # type: ignore[union-attr]
    assert await tasks.delete_account(ctx, user_id=str(user.id), scheduled_ts=ts) == "not_due"

    await db_session.execute(
        update(User)
        .where(User.id == user.id)
        .values(deletion_scheduled_for=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()
    assert (
        await tasks.delete_account(ctx, user_id=str(user.id), scheduled_ts=ts) == "skipped"
    )  # stale job
    assert await tasks.purge_due_deletions(ctx) == 1  # the daily sweep catches it

    db_session.expire_all()
    gone = await db_session.get(User, chat.client.id)
    assert gone is not None
    assert gone.email == f"deleted-{gone.id.hex}@deleted.invalid"
    assert gone.password_hash is None
    assert gone.deleted_at is not None
    profile = await db_session.get(ClientProfile, chat.client.id)
    assert profile is not None
    assert profile.full_name == "Deleted user"
    assert profile.phone is None
    assert profile.avatar_key is None
    assert storage.objects == {}
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Notification).where(Notification.user_id == gone.id)
        )
        == 0
    )

    # Kept: the case, the booking and what they wrote, now shown as "Deleted user".
    assert await db_session.scalar(select(Case.client_id).where(Case.id == chat.case_id)) == gone.id
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Booking).where(Booking.client_id == gone.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(select(ChatMessage.body).where(ChatMessage.sender_id == gone.id))
        == "My statement for the record"
    )
    detail = (await client.get(f"{API}/cases/{chat.case_id}", headers=chat.lead.headers)).json()
    assert detail["client"]["full_name"] == "Deleted user"

    assert jobs.emails_to("owner@example.com")[-1]["subject"] == "Your account has been deleted"
    # The address is free again.
    await register_client(client, "owner@example.com")

    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    actions = {
        e["action"]
        for e in (await client.get(f"{API}/admin/audit-logs", headers=admin.headers)).json()[
            "items"
        ]
    }
    assert {"user.deletion_requested", "user.deleted"} <= actions


async def test_export_contains_only_my_data(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    resp = await client.get(f"{API}/users/me/export", headers=chat.client.headers)
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith('attachment; filename="legalpro-export-')
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()
    assert data["account"]["email"] == "owner@example.com"
    assert data["profile"]["full_name"] == "Owner"
    assert [b["your_role"] for b in data["bookings"]] == ["client"]
    assert data["payments"] == []
    assert "other@example.com" not in resp.text
    assert "Secret merger" not in resp.text  # case content isn't "my profile/bookings/payments"

    advocate = (await client.get(f"{API}/users/me/export", headers=chat.lead.headers)).json()
    assert advocate["profile"]["bar_council_id"].startswith("BAR/")
    assert [b["your_role"] for b in advocate["bookings"]] == ["advocate"]

    for _ in range(4):
        await client.get(f"{API}/users/me/export", headers=chat.client.headers)
    limited = await client.get(f"{API}/users/me/export", headers=chat.client.headers)
    assert limited.status_code == 429
