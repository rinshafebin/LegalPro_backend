"""Admin access: MFA required, user management, every action audited, no case content."""

from typing import Any

import pyotp
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_str, hash_password
from app.modules.users.models import User, UserRole
from tests.chat_support import ChatWorld, chat
from tests.helpers import API, PASSWORD, Actor, bearer, make_advocate, make_user, totp_code

__all__ = ["chat"]


async def _real_admin(db_session: AsyncSession, *, with_mfa: bool) -> str | None:
    secret = pyotp.random_base32() if with_mfa else None
    db_session.add(
        User(
            email="boss@example.com",
            password_hash=await hash_password(PASSWORD),
            role=UserRole.ADMIN,
            email_verified=True,
            mfa_enabled=with_mfa,
            mfa_secret_encrypted=encrypt_str(secret) if secret else None,
        )
    )
    await db_session.commit()
    return secret


async def _logs(client: AsyncClient, admin: Actor, action: str) -> list[dict[str, Any]]:
    resp = await client.get(
        f"{API}/admin/audit-logs", headers=admin.headers, params={"action": action}
    )
    items: list[dict[str, Any]] = resp.json()["items"]
    return items


async def test_admin_without_mfa_can_log_in_but_not_use_admin_endpoints(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _real_admin(db_session, with_mfa=False)
    login = await client.post(
        f"{API}/auth/login", json={"email": "boss@example.com", "password": PASSWORD}
    )
    headers = bearer(login.json()["tokens"]["access_token"])
    for url in ("/admin/users", "/admin/refunds", "/admin/audit-logs", "/admin/advocates"):
        resp = await client.get(f"{API}{url}", headers=headers)
        assert resp.status_code == 403, url
        assert resp.json()["error"]["code"] == "admin_mfa_required"
    # ...but can still set MFA up.
    assert (await client.post(f"{API}/auth/mfa/setup", headers=headers)).status_code == 200


async def test_admin_with_mfa_gets_access_only_through_the_totp_step(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    secret = await _real_admin(db_session, with_mfa=True)
    assert secret is not None
    login = (
        await client.post(
            f"{API}/auth/login", json={"email": "boss@example.com", "password": PASSWORD}
        )
    ).json()
    assert login["mfa_required"] is True
    verified = (
        await client.post(
            f"{API}/auth/mfa/verify",
            json={"mfa_token": login["mfa_token"], "code": totp_code(secret)},
        )
    ).json()
    headers = bearer(verified["tokens"]["access_token"])
    assert (await client.get(f"{API}/admin/users", headers=headers)).status_code == 200

    # The MFA mark survives refresh.
    refreshed = await client.post(
        f"{API}/auth/token/refresh", json={"refresh_token": verified["tokens"]["refresh_token"]}
    )
    headers = bearer(refreshed.json()["access_token"])
    assert (await client.get(f"{API}/admin/users", headers=headers)).status_code == 200


async def test_list_deactivate_reactivate_are_audited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    target = await make_user(db_session, UserRole.CLIENT, "target@example.com")
    await make_advocate(db_session, "adv@example.com")

    listed = await client.get(
        f"{API}/admin/users", headers=admin.headers, params={"role": "client", "q": "target"}
    )
    assert [u["email"] for u in listed.json()["items"]] == ["target@example.com"]

    url = f"{API}/admin/users/{target.id}"
    resp = await client.post(
        f"{url}/deactivate", headers=admin.headers, json={"reason": "fraud report"}
    )
    assert resp.json()["is_active"] is False
    assert (await client.get(f"{API}/users/me", headers=target.headers)).status_code == 401
    selfie = await client.post(
        f"{API}/admin/users/{admin.id}/deactivate", headers=admin.headers, json={"reason": "oops"}
    )
    assert selfie.json()["error"]["code"] == "cannot_deactivate_self"
    assert (await client.post(f"{url}/reactivate", headers=admin.headers)).json()[
        "is_active"
    ] is True

    [deact] = await _logs(client, admin, "user.deactivated")
    assert deact["entity_id"] == str(target.id)
    assert deact["details"] == {"reason": "fraud report"}
    assert len(await _logs(client, admin, "user.reactivated")) == 1
    viewed = await _logs(client, admin, "admin.viewed")
    assert {v["details"]["path"] for v in viewed} >= {
        "/api/v1/admin/users",
        "/api/v1/admin/audit-logs",
    }


async def test_advocate_verification_and_specializations_are_audited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    adv = await make_advocate(db_session, "new@example.com", verified=False)
    await client.post(f"{API}/admin/advocates/{adv.id}/verify", headers=admin.headers)
    await client.post(f"{API}/admin/advocates/{adv.id}/unverify", headers=admin.headers)
    await client.post(
        f"{API}/specializations",
        headers=admin.headers,
        json={"slug": "space-law", "name": "Space law"},
    )
    for action in ("advocate.verified", "advocate.unverified"):
        [entry] = await _logs(client, admin, action)
        assert entry["entity_id"] == str(adv.id)
        assert entry["actor_id"] == str(admin.id)
    [spec] = await _logs(client, admin, "specialization.created")
    assert spec["details"] == {"slug": "space-law"}


async def test_admins_still_cant_read_case_content(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    for url in (
        f"/cases/{chat.case_id}",
        f"/cases/{chat.case_id}/notes",
        f"/cases/{chat.case_id}/documents",
        f"/chat/rooms/{chat.room_id}/messages",
        "/cases",
        "/chat/rooms",
    ):
        assert (await client.get(f"{API}{url}", headers=admin.headers)).status_code == 403, url
    logs = (await client.get(f"{API}/admin/audit-logs", headers=admin.headers)).json()["items"]
    assert "Secret merger" not in str(logs)
