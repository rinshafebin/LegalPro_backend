import re
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import create_email_verification_token
from app.modules.users.models import UserRole
from tests.conftest import FakeJobQueue
from tests.helpers import API, bearer, make_user, register_advocate, register_client


def _token_from_email(jobs: FakeJobQueue, email: str) -> str:
    body = jobs.emails_to(email)[-1]["body"]
    match = re.search(r"/verify-email\?token=(\S+)", body)
    assert match, body
    return match.group(1)


async def test_registration_sends_verification_link_and_verify_works(
    client: AsyncClient, jobs: FakeJobQueue
) -> None:
    data = await register_client(client)
    assert data["user"]["email_verified"] is False
    body = jobs.emails_to("client@example.com")[0]["body"]
    assert body.count("http://localhost:5173/verify-email?token=") == 1

    token = _token_from_email(jobs, "client@example.com")
    resp = await client.post(f"{API}/auth/email/verify", json={"token": token})
    assert resp.status_code == 200

    me = await client.get(f"{API}/users/me", headers=bearer(data["tokens"]["access_token"]))
    assert me.json()["email_verified"] is True

    # Re-using the link is harmless (idempotent).
    again = await client.post(f"{API}/auth/email/verify", json={"token": token})
    assert again.status_code == 200


async def test_tampered_verification_token_is_rejected(
    client: AsyncClient, jobs: FakeJobQueue
) -> None:
    await register_client(client)
    token = _token_from_email(jobs, "client@example.com")
    resp = await client.post(f"{API}/auth/email/verify", json={"token": token[:-3] + "xyz"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_token"


async def test_expired_verification_token_is_rejected(
    client: AsyncClient, jobs: FakeJobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register_client(client)
    token = _token_from_email(jobs, "client@example.com")
    monkeypatch.setattr(get_settings(), "email_verification_ttl_hours", -1)

    resp = await client.post(f"{API}/auth/email/verify", json={"token": token})
    assert resp.status_code == 400
    assert "expired" in resp.json()["error"]["message"]


async def test_verification_token_is_bound_to_the_email_address(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, UserRole.CLIENT, "now@example.com", email_verified=False)
    stale = create_email_verification_token(user.id, "old-address@example.com")
    resp = await client.post(f"{API}/auth/email/verify", json={"token": stale})
    assert resp.status_code == 400

    unknown = create_email_verification_token(uuid.uuid4(), "now@example.com")
    resp = await client.post(f"{API}/auth/email/verify", json={"token": unknown})
    assert resp.status_code == 400


async def test_resend_verification(client: AsyncClient, jobs: FakeJobQueue) -> None:
    data = await register_client(client)
    headers = bearer(data["tokens"]["access_token"])

    resp = await client.post(f"{API}/auth/email/verify/resend", headers=headers)
    assert resp.status_code == 202
    assert len(jobs.emails_to("client@example.com")) == 2

    token = _token_from_email(jobs, "client@example.com")
    await client.post(f"{API}/auth/email/verify", json={"token": token})
    resp = await client.post(f"{API}/auth/email/verify/resend", headers=headers)
    assert resp.status_code == 409


async def test_resend_verification_is_rate_limited_per_user(client: AsyncClient) -> None:
    data = await register_client(client)
    headers = bearer(data["tokens"]["access_token"])
    statuses = [
        (await client.post(f"{API}/auth/email/verify/resend", headers=headers)).status_code
        for _ in range(4)
    ]
    assert statuses == [202, 202, 202, 429]


async def test_admin_cannot_verify_advocate_before_email_is_verified(
    client: AsyncClient, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    data = await register_advocate(client)
    advocate_id = data["user"]["id"]
    verify_url = f"{API}/admin/advocates/{advocate_id}/verify"

    blocked = await client.post(verify_url, headers=admin.headers)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "email_not_verified"

    token = _token_from_email(jobs, "advocate@example.com")
    assert (await client.post(f"{API}/auth/email/verify", json={"token": token})).status_code == 200

    ok = await client.post(verify_url, headers=admin.headers)
    assert ok.status_code == 200
    assert ok.json()["is_verified"] is True
    assert ok.json()["verified_at"] is not None
    subjects = [m["subject"] for m in jobs.emails_to("advocate@example.com")]
    assert "Your advocate profile has been verified" in subjects

    # Idempotent: verifying again doesn't send another email.
    again = await client.post(verify_url, headers=admin.headers)
    assert again.status_code == 200
    subjects = [m["subject"] for m in jobs.emails_to("advocate@example.com")]
    assert subjects.count("Your advocate profile has been verified") == 1


async def test_google_accounts_are_email_verified(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, jobs: FakeJobQueue
) -> None:
    from typing import Any

    from app.modules.auth import google_oauth

    async def _verify(token: str, client_id: str) -> dict[str, Any]:
        return {"email": "g@example.com", "email_verified": True, "name": "G"}

    monkeypatch.setattr(google_oauth, "verify_id_token", _verify)
    resp = await client.post(f"{API}/auth/login/google", json={"id_token": "x" * 20})
    assert resp.json()["user"]["email_verified"] is True
    # Google sign-up sends a plain welcome, not a verification link.
    assert "verify-email" not in jobs.emails_to("g@example.com")[0]["body"]
