import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenType, create_token, decrypt_str
from app.modules.auth import google_oauth
from app.modules.auth.models import PasswordResetCode
from app.modules.users.models import User
from tests.conftest import FakeJobQueue
from tests.helpers import (
    API,
    PASSWORD,
    bearer,
    login,
    register_advocate,
    register_client,
    totp_code,
)

# ---------------------------------------------------------------- registration


async def test_register_client_returns_tokens_and_queues_welcome_email(
    client: AsyncClient, jobs: FakeJobQueue
) -> None:
    data = await register_client(client, email="Alice@Example.COM", phone="+91 98765 43210")

    assert data["user"]["email"] == "alice@example.com"  # normalized
    assert data["user"]["role"] == "client"
    assert data["user"]["profile"] == {
        "full_name": "Client User",
        "phone": "+91 98765 43210",
        "bar_council_id": None,
        "is_verified": None,
    }
    assert data["tokens"]["access_token"]
    assert data["tokens"]["refresh_token"]
    assert len(jobs.emails_to("alice@example.com")) == 1


async def test_register_rejects_duplicate_email_case_insensitively(client: AsyncClient) -> None:
    await register_client(client, email="dup@example.com")
    resp = await client.post(
        f"{API}/auth/register/client",
        json={"email": "DUP@example.com", "password": PASSWORD, "full_name": "Someone Else"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("short1", "at least 8"),
        ("12345678901", "entirely numeric"),
        ("onlyletters", "letter and one digit"),
        ("password123", "too common"),
        ("weakuser99x", "similar to the email"),
    ],
)
async def test_register_rejects_weak_passwords(
    client: AsyncClient, password: str, reason: str
) -> None:
    resp = await client.post(
        f"{API}/auth/register/client",
        json={"email": "weakuser@example.com", "password": password, "full_name": "Weak"},
    )
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "validation_error"
    assert reason in str(body["details"])


async def test_register_advocate_is_unverified_and_bar_id_unique(client: AsyncClient) -> None:
    data = await register_advocate(client, bar_council_id="kl/99/2019")
    assert data["user"]["role"] == "advocate"
    assert data["user"]["profile"]["bar_council_id"] == "KL/99/2019"
    assert data["user"]["profile"]["is_verified"] is False

    resp = await client.post(
        f"{API}/auth/register/advocate",
        json={
            "email": "other@example.com",
            "password": PASSWORD,
            "full_name": "Other Advocate",
            "bar_council_id": "KL/99/2019",
        },
    )
    assert resp.status_code == 409
    assert "bar council" in resp.json()["error"]["message"].lower()


async def test_register_cannot_choose_role(client: AsyncClient) -> None:
    resp = await client.post(
        f"{API}/auth/register/client",
        json={
            "email": "sneaky@example.com",
            "password": PASSWORD,
            "full_name": "Sneaky",
            "role": "admin",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["user"]["role"] == "client"


# ---------------------------------------------------------------- login


async def test_login_success_and_me(client: AsyncClient) -> None:
    await register_client(client)
    data = await login(client, "client@example.com")
    assert data["mfa_required"] is False

    me = await client.get(f"{API}/users/me", headers=bearer(data["tokens"]["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "client@example.com"


async def test_login_failures_share_one_generic_message(client: AsyncClient) -> None:
    await register_client(client)
    wrong_password = await client.post(
        f"{API}/auth/login", json={"email": "client@example.com", "password": "Wrong-pass-1"}
    )
    unknown_email = await client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": "Wrong-pass-1"}
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_disabled_account_cannot_log_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await register_client(client)
    await db_session.execute(update(User).values(is_active=False))
    await db_session.commit()

    resp = await client.post(
        f"{API}/auth/login", json={"email": "client@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------- tokens


async def test_me_requires_a_valid_access_token(client: AsyncClient) -> None:
    data = await register_client(client)
    user_id = data["user"]["id"]

    assert (await client.get(f"{API}/users/me")).status_code == 401

    tampered = data["tokens"]["access_token"][:-4] + "abcd"
    assert (await client.get(f"{API}/users/me", headers=bearer(tampered))).status_code == 401

    # A refresh token is not an access token.
    refresh = data["tokens"]["refresh_token"]
    assert (await client.get(f"{API}/users/me", headers=bearer(refresh))).status_code == 401

    expired, _ = create_token(uuid.UUID(user_id), TokenType.ACCESS, timedelta(seconds=-1))
    resp = await client.get(f"{API}/users/me", headers=bearer(expired))
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Token has expired"


async def test_refresh_rotates_and_detects_reuse(client: AsyncClient) -> None:
    data = await register_client(client)
    first_refresh = data["tokens"]["refresh_token"]

    rotated = await client.post(f"{API}/auth/token/refresh", json={"refresh_token": first_refresh})
    assert rotated.status_code == 200
    second_refresh = rotated.json()["refresh_token"]
    assert second_refresh != first_refresh

    # Replaying the old token is treated as theft: it fails AND kills the new session too.
    replay = await client.post(f"{API}/auth/token/refresh", json={"refresh_token": first_refresh})
    assert replay.status_code == 401
    after = await client.post(f"{API}/auth/token/refresh", json={"refresh_token": second_refresh})
    assert after.status_code == 401


async def test_access_token_is_not_accepted_as_refresh_token(client: AsyncClient) -> None:
    data = await register_client(client)
    resp = await client.post(
        f"{API}/auth/token/refresh", json={"refresh_token": data["tokens"]["access_token"]}
    )
    assert resp.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient) -> None:
    data = await register_client(client)
    refresh = data["tokens"]["refresh_token"]
    assert (
        await client.post(f"{API}/auth/logout", json={"refresh_token": refresh})
    ).status_code == 204
    resp = await client.post(f"{API}/auth/token/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401


async def test_logout_all_revokes_access_and_refresh_tokens(client: AsyncClient) -> None:
    data = await register_client(client)
    other_session = await login(client, "client@example.com")
    headers = bearer(data["tokens"]["access_token"])

    assert (await client.post(f"{API}/auth/logout/all", headers=headers)).status_code == 204

    assert (await client.get(f"{API}/users/me", headers=headers)).status_code == 401
    other_refresh = other_session["tokens"]["refresh_token"]
    resp = await client.post(f"{API}/auth/token/refresh", json={"refresh_token": other_refresh})
    assert resp.status_code == 401


# ---------------------------------------------------------------- /users/me


async def test_patch_me_updates_profile(client: AsyncClient) -> None:
    data = await register_advocate(client)
    headers = bearer(data["tokens"]["access_token"])

    resp = await client.patch(
        f"{API}/users/me",
        headers=headers,
        json={"full_name": "Adv. New Name", "phone": "9876543210"},
    )
    assert resp.status_code == 200
    assert resp.json()["profile"]["full_name"] == "Adv. New Name"
    assert resp.json()["profile"]["phone"] == "9876543210"

    # Fields outside the schema (e.g. role, is_verified) are rejected, not silently applied.
    resp = await client.patch(f"{API}/users/me", headers=headers, json={"is_verified": True})
    assert resp.status_code == 422


# ---------------------------------------------------------------- MFA


async def _enable_mfa(client: AsyncClient, access_token: str) -> str:
    headers = bearer(access_token)
    setup = await client.post(f"{API}/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200
    body = setup.json()
    assert body["qr_code_data_uri"].startswith("data:image/png;base64,")
    secret: str = body["secret"]

    confirm = await client.post(
        f"{API}/auth/mfa/confirm", headers=headers, json={"code": totp_code(secret)}
    )
    assert confirm.status_code == 204
    return secret


async def test_mfa_setup_does_not_enable_until_confirmed(client: AsyncClient) -> None:
    data = await register_advocate(client)
    headers = bearer(data["tokens"]["access_token"])

    setup = (await client.post(f"{API}/auth/mfa/setup", headers=headers)).json()
    # Not confirmed yet: login still returns tokens directly, so a user who never
    # scanned the QR code can't lock themselves out.
    assert (await login(client, "advocate@example.com"))["mfa_required"] is False

    wrong = await client.post(f"{API}/auth/mfa/confirm", headers=headers, json={"code": "000000"})
    assert wrong.status_code == 400

    ok = await client.post(
        f"{API}/auth/mfa/confirm", headers=headers, json={"code": totp_code(setup["secret"])}
    )
    assert ok.status_code == 204
    assert (await login(client, "advocate@example.com"))["mfa_required"] is True


async def test_mfa_secret_is_encrypted_at_rest(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    data = await register_advocate(client)
    secret = await _enable_mfa(client, data["tokens"]["access_token"])

    user = await db_session.scalar(select(User).where(User.email == "advocate@example.com"))
    assert user is not None
    assert user.mfa_secret_encrypted is not None
    assert secret not in user.mfa_secret_encrypted
    assert decrypt_str(user.mfa_secret_encrypted) == secret


async def test_mfa_login_flow(client: AsyncClient) -> None:
    data = await register_advocate(client)
    secret = await _enable_mfa(client, data["tokens"]["access_token"])

    challenge = await login(client, "advocate@example.com")
    assert challenge["mfa_required"] is True
    assert challenge["tokens"] is None
    assert "user_id" not in challenge

    mfa_token = challenge["mfa_token"]
    # The MFA token is not an access token.
    assert (await client.get(f"{API}/users/me", headers=bearer(mfa_token))).status_code == 401

    resp = await client.post(
        f"{API}/auth/mfa/verify", json={"mfa_token": mfa_token, "code": totp_code(secret, 1)}
    )
    assert resp.status_code == 200
    tokens = resp.json()["tokens"]
    assert (
        await client.get(f"{API}/users/me", headers=bearer(tokens["access_token"]))
    ).status_code == 200

    # The MFA token is single-use.
    reuse = await client.post(
        f"{API}/auth/mfa/verify", json={"mfa_token": mfa_token, "code": totp_code(secret, -1)}
    )
    assert reuse.status_code == 401


async def test_mfa_verify_rejects_raw_user_id(client: AsyncClient) -> None:
    data = await register_advocate(client)
    resp = await client.post(
        f"{API}/auth/mfa/verify", json={"user_id": data["user"]["id"], "code": "123456"}
    )
    assert resp.status_code == 422


async def test_totp_code_cannot_be_replayed(client: AsyncClient) -> None:
    data = await register_advocate(client)
    secret = await _enable_mfa(client, data["tokens"]["access_token"])
    code = totp_code(secret, 1)

    first = (await login(client, "advocate@example.com"))["mfa_token"]
    ok = await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": first, "code": code})
    assert ok.status_code == 200

    second = (await login(client, "advocate@example.com"))["mfa_token"]
    replay = await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": second, "code": code})
    assert replay.status_code == 401


async def test_mfa_token_is_burned_after_too_many_wrong_codes(client: AsyncClient) -> None:
    data = await register_advocate(client)
    secret = await _enable_mfa(client, data["tokens"]["access_token"])
    mfa_token = (await login(client, "advocate@example.com"))["mfa_token"]

    for _ in range(5):
        r = await client.post(
            f"{API}/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"}
        )
        assert r.status_code == 401

    # Even the correct code no longer works with this token.
    r = await client.post(
        f"{API}/auth/mfa/verify", json={"mfa_token": mfa_token, "code": totp_code(secret, 1)}
    )
    assert r.status_code == 401
    assert "no longer valid" in r.json()["error"]["message"]


async def test_mfa_disable_requires_code(client: AsyncClient) -> None:
    data = await register_advocate(client)
    secret = await _enable_mfa(client, data["tokens"]["access_token"])
    headers = bearer(data["tokens"]["access_token"])

    bad = await client.post(f"{API}/auth/mfa/disable", headers=headers, json={"code": "000000"})
    assert bad.status_code == 400
    ok = await client.post(
        f"{API}/auth/mfa/disable", headers=headers, json={"code": totp_code(secret, 1)}
    )
    assert ok.status_code == 204
    assert (await login(client, "advocate@example.com"))["mfa_required"] is False


# ---------------------------------------------------------------- password reset


def _code_from_email(jobs: FakeJobQueue, email: str) -> str:
    body = jobs.emails_to(email)[-1]["body"]
    match = re.search(r"\b(\d{6})\b", body)
    assert match
    return match.group(1)


async def test_forgot_password_does_not_reveal_unknown_emails(
    client: AsyncClient, jobs: FakeJobQueue
) -> None:
    await register_client(client)
    known = await client.post(f"{API}/auth/password/forgot", json={"email": "client@example.com"})
    unknown = await client.post(f"{API}/auth/password/forgot", json={"email": "ghost@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(jobs.emails_to("client@example.com")) == 2  # welcome + reset
    assert jobs.emails_to("ghost@example.com") == []


async def test_reset_code_is_stored_hashed(
    client: AsyncClient, jobs: FakeJobQueue, db_session: AsyncSession
) -> None:
    await register_client(client)
    await client.post(f"{API}/auth/password/forgot", json={"email": "client@example.com"})
    code = _code_from_email(jobs, "client@example.com")

    record = await db_session.scalar(select(PasswordResetCode))
    assert record is not None
    assert code not in record.code_hash
    assert len(record.code_hash) == 64


async def test_password_reset_flow_revokes_existing_sessions(
    client: AsyncClient, jobs: FakeJobQueue
) -> None:
    data = await register_client(client)
    old_headers = bearer(data["tokens"]["access_token"])
    old_refresh = data["tokens"]["refresh_token"]

    await client.post(f"{API}/auth/password/forgot", json={"email": "client@example.com"})
    code = _code_from_email(jobs, "client@example.com")
    new_password = "Brand-new-pass-42"

    resp = await client.post(
        f"{API}/auth/password/reset",
        json={"email": "client@example.com", "code": code, "new_password": new_password},
    )
    assert resp.status_code == 204

    # Old password fails, new one works.
    old = await client.post(
        f"{API}/auth/login", json={"email": "client@example.com", "password": PASSWORD}
    )
    assert old.status_code == 401
    await login(client, "client@example.com", new_password)

    # Sessions from before the reset are dead.
    assert (await client.get(f"{API}/users/me", headers=old_headers)).status_code == 401
    refresh = await client.post(f"{API}/auth/token/refresh", json={"refresh_token": old_refresh})
    assert refresh.status_code == 401

    # The code is single-use.
    again = await client.post(
        f"{API}/auth/password/reset",
        json={"email": "client@example.com", "code": code, "new_password": "Another-pass-43"},
    )
    assert again.status_code == 400


async def test_reset_code_expires(
    client: AsyncClient, jobs: FakeJobQueue, db_session: AsyncSession
) -> None:
    await register_client(client)
    await client.post(f"{API}/auth/password/forgot", json={"email": "client@example.com"})
    code = _code_from_email(jobs, "client@example.com")

    await db_session.execute(
        update(PasswordResetCode).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()

    resp = await client.post(
        f"{API}/auth/password/reset",
        json={"email": "client@example.com", "code": code, "new_password": "Brand-new-pass-42"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_code"


async def test_reset_code_locks_after_too_many_wrong_attempts(
    client: AsyncClient, jobs: FakeJobQueue
) -> None:
    await register_client(client)
    await client.post(f"{API}/auth/password/forgot", json={"email": "client@example.com"})
    code = _code_from_email(jobs, "client@example.com")
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(5):
        r = await client.post(
            f"{API}/auth/password/reset",
            json={
                "email": "client@example.com",
                "code": wrong,
                "new_password": "Brand-new-pass-42",
            },
        )
        assert r.status_code == 400

    r = await client.post(
        f"{API}/auth/password/reset",
        json={"email": "client@example.com", "code": code, "new_password": "Brand-new-pass-42"},
    )
    assert r.status_code == 400


async def test_new_reset_code_supersedes_old_one(client: AsyncClient, jobs: FakeJobQueue) -> None:
    await register_client(client)
    await client.post(f"{API}/auth/password/forgot", json={"email": "client@example.com"})
    first = _code_from_email(jobs, "client@example.com")
    await client.post(f"{API}/auth/password/forgot", json={"email": "client@example.com"})
    second = _code_from_email(jobs, "client@example.com")

    if first != second:
        r = await client.post(
            f"{API}/auth/password/reset",
            json={
                "email": "client@example.com",
                "code": first,
                "new_password": "Brand-new-pass-42",
            },
        )
        assert r.status_code == 400
    r = await client.post(
        f"{API}/auth/password/reset",
        json={"email": "client@example.com", "code": second, "new_password": "Brand-new-pass-42"},
    )
    assert r.status_code == 204


# ---------------------------------------------------------------- Google


def _fake_google(claims: dict[str, Any] | Exception) -> Any:
    async def _verify(token: str, client_id: str) -> dict[str, Any]:
        assert client_id == "test-google-client-id"
        if isinstance(claims, Exception):
            raise claims
        return claims

    return _verify


async def test_google_login_creates_verified_client(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, jobs: FakeJobQueue
) -> None:
    monkeypatch.setattr(
        google_oauth,
        "verify_id_token",
        _fake_google({"email": "G.User@gmail.com", "email_verified": True, "name": "G User"}),
    )
    resp = await client.post(f"{API}/auth/login/google", json={"id_token": "x" * 20})
    assert resp.status_code == 200
    user = resp.json()["user"]
    assert user["email"] == "g.user@gmail.com"
    assert user["role"] == "client"
    assert user["email_verified"] is True
    assert user["profile"]["full_name"] == "G User"
    assert len(jobs.emails_to("g.user@gmail.com")) == 1

    # Google-created accounts have no password, so password login is impossible.
    pw = await client.post(
        f"{API}/auth/login", json={"email": "g.user@gmail.com", "password": PASSWORD}
    )
    assert pw.status_code == 401


async def test_google_login_respects_mfa(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = await register_advocate(client)
    await _enable_mfa(client, data["tokens"]["access_token"])
    monkeypatch.setattr(
        google_oauth,
        "verify_id_token",
        _fake_google({"email": "advocate@example.com", "email_verified": True}),
    )
    resp = await client.post(f"{API}/auth/login/google", json={"id_token": "x" * 20})
    assert resp.status_code == 200
    assert resp.json()["mfa_required"] is True
    assert resp.json()["tokens"] is None


@pytest.mark.parametrize(
    ("claims", "status"),
    [
        (ValueError("bad signature"), 401),
        ({"email": "x@gmail.com", "email_verified": False}, 400),
        ({"email_verified": True}, 400),
    ],
)
async def test_google_login_rejects_bad_tokens(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    claims: dict[str, Any] | Exception,
    status: int,
) -> None:
    monkeypatch.setattr(google_oauth, "verify_id_token", _fake_google(claims))
    resp = await client.post(f"{API}/auth/login/google", json={"id_token": "x" * 20})
    assert resp.status_code == status
    assert "error" in resp.json()


# ---------------------------------------------------------------- rate limiting


async def test_login_is_rate_limited(client: AsyncClient) -> None:
    body = {"email": "victim@example.com", "password": "Wrong-pass-1"}
    for _ in range(10):
        assert (await client.post(f"{API}/auth/login", json=body)).status_code == 401

    blocked = await client.post(f"{API}/auth/login", json=body)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) > 0


async def test_rate_limit_ignores_spoofed_forwarded_for(client: AsyncClient) -> None:
    """A client must not be able to reset its bucket by inventing X-Forwarded-For values."""
    for i in range(10):
        r = await client.post(
            f"{API}/auth/login",
            json={"email": f"user{i}@example.com", "password": "Wrong-pass-1"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
        assert r.status_code == 401

    r = await client.post(
        f"{API}/auth/login",
        json={"email": "fresh@example.com", "password": "Wrong-pass-1"},
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    assert r.status_code == 429


async def test_login_is_rate_limited_per_account_across_ips(
    client: AsyncClient, redis: Any
) -> None:
    body = {"email": "target@example.com", "password": "Wrong-pass-1"}
    for _ in range(10):
        assert (await client.post(f"{API}/auth/login", json=body)).status_code == 401
        await redis.delete("rl:login:ip:127.0.0.1")  # simulate a fresh IP every attempt

    assert (await client.post(f"{API}/auth/login", json=body)).status_code == 429


async def _login_attempts_via_proxy(
    db_session: AsyncSession, peer_ip: str, trusted_proxies: str
) -> list[int]:
    """Run the app behind uvicorn's real ProxyHeadersMiddleware (as in production)."""
    from httpx import ASGITransport
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.core.db import get_session
    from app.main import app

    async def _session_override() -> Any:
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted_proxies)  # type: ignore[arg-type]
    transport = ASGITransport(app=wrapped, client=(peer_ip, 50000))  # type: ignore[arg-type]
    statuses = []
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            for i in range(11):
                r = await c.post(
                    f"{API}/auth/login",
                    json={"email": f"user{i}@example.com", "password": "Wrong-pass-1"},
                    headers={"X-Forwarded-For": f"198.51.100.{i}"},
                )
                statuses.append(r.status_code)
    finally:
        app.dependency_overrides.clear()
    return statuses


async def test_untrusted_peer_cannot_spoof_forwarded_for(db_session: AsyncSession) -> None:
    # Peer is not a trusted proxy, so its X-Forwarded-For is ignored: one bucket, 11th blocked.
    statuses = await _login_attempts_via_proxy(db_session, "203.0.113.5", "127.0.0.1")
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429


async def test_trusted_proxy_forwarded_for_is_honoured(db_session: AsyncSession) -> None:
    # Peer IS the trusted proxy, so each forwarded client IP gets its own bucket.
    statuses = await _login_attempts_via_proxy(db_session, "10.0.0.2", "10.0.0.2")
    assert statuses == [401] * 11


async def test_queued_email_payload_hides_secrets_and_pii(
    client: AsyncClient, jobs: FakeJobQueue
) -> None:
    """Job args are stored in Redis and printed by the worker, so they must be opaque."""
    await register_client(client)
    await client.post(f"{API}/auth/password/forgot", json={"email": "client@example.com"})
    code = _code_from_email(jobs, "client@example.com")

    raw_args = str([kwargs for _, kwargs in jobs.jobs])
    assert code not in raw_args
    assert "client@example.com" not in raw_args


async def test_send_email_job_decrypts_and_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import email
    from app.workers import tasks

    sent: list[tuple[str, str, str]] = []

    async def _fake_send(to: str, subject: str, body: str) -> None:
        sent.append((to, subject, body))

    monkeypatch.setattr(email, "send_email", _fake_send)
    await tasks.send_email({}, sealed=email.seal_message("a@example.com", "Hi", "Body 123"))
    assert sent == [("a@example.com", "Hi", "Body 123")]
