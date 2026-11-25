"""Shared test helpers for creating users and logging in through the API."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenType, create_token
from app.modules.advocates.models import AdvocateProfile, Specialization
from app.modules.clients.models import ClientProfile
from app.modules.users.models import User, UserRole

API = "/api/v1"
PASSWORD = "Str0ng-pass-phrase"


async def register_client(
    client: AsyncClient, email: str = "client@example.com", **extra: Any
) -> dict[str, Any]:
    body = {"email": email, "password": PASSWORD, "full_name": "Client User", **extra}
    resp = await client.post(f"{API}/auth/register/client", json=body)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def register_advocate(
    client: AsyncClient,
    email: str = "advocate@example.com",
    bar_council_id: str = "KL/1234/2020",
    **extra: Any,
) -> dict[str, Any]:
    body = {
        "email": email,
        "password": PASSWORD,
        "full_name": "Advocate User",
        "bar_council_id": bar_council_id,
        **extra,
    }
    resp = await client.post(f"{API}/auth/register/advocate", json=body)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def login(client: AsyncClient, email: str, password: str = PASSWORD) -> dict[str, Any]:
    resp = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    return data


def bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def totp_code(secret: str, step: int = 0) -> str:
    """Code for the current 30 s window (step=0) or a neighbouring one (±1).

    Each code can only be used once, so tests that need two codes use step 0 and then 1.
    """
    totp = pyotp.TOTP(secret)
    return str(totp.at(datetime.now(UTC) + timedelta(seconds=step * totp.interval)))


# ---------------------------------------------------------------- direct DB factories


@dataclass(frozen=True)
class Actor:
    """Plain snapshot of a created user (ORM objects get expired between requests)."""

    id: uuid.UUID
    email: str
    headers: dict[str, str]


def _headers_for(user: User) -> dict[str, str]:
    token, _ = create_token(
        user.id,
        TokenType.ACCESS,
        timedelta(minutes=15),
        version=user.token_version,
        role=user.role.value,
    )
    return bearer(token)


async def make_user(
    session: AsyncSession,
    role: UserRole,
    email: str,
    *,
    email_verified: bool = True,
    is_active: bool = True,
    full_name: str = "Test User",
) -> Actor:
    user = User(
        email=email,
        password_hash=None,
        role=role,
        email_verified=email_verified,
        is_active=is_active,
    )
    if role is UserRole.CLIENT:
        user.client_profile = ClientProfile(full_name=full_name)
    session.add(user)
    await session.commit()
    return Actor(id=user.id, email=user.email, headers=_headers_for(user))


async def make_specialization(session: AsyncSession, slug: str, name: str) -> None:
    """Get-or-create (the migrations seed common practice areas)."""
    if await session.scalar(select(Specialization.id).where(Specialization.slug == slug)):
        return
    session.add(Specialization(slug=slug, name=name))
    await session.commit()


async def make_advocate(
    session: AsyncSession,
    email: str,
    *,
    full_name: str = "Adv. Test",
    verified: bool = True,
    email_verified: bool = True,
    is_active: bool = True,
    city: str | None = None,
    languages: list[str] | None = None,
    experience_years: int = 0,
    specialization_slugs: list[str] | None = None,
) -> Actor:
    specs = []
    if specialization_slugs:
        specs = list(
            (
                await session.scalars(
                    select(Specialization).where(Specialization.slug.in_(specialization_slugs))
                )
            ).all()
        )
    user = User(
        email=email,
        password_hash=None,
        role=UserRole.ADVOCATE,
        email_verified=email_verified,
        is_active=is_active,
    )
    user.advocate_profile = AdvocateProfile(
        full_name=full_name,
        bar_council_id=f"BAR/{uuid.uuid4().hex[:10].upper()}",
        is_verified=verified,
        city=city,
        languages=languages or [],
        experience_years=experience_years,
        specializations=specs,
    )
    session.add(user)
    await session.commit()
    return Actor(id=user.id, email=user.email, headers=_headers_for(user))


def webhook_body_refund(
    refund_id: str,
    payment_id: str,
    amount: int,
    *,
    event: str = "refund.processed",
    event_id: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """A signed Razorpay refund webhook delivery."""
    import json

    from app.modules.payments.gateway import webhook_signature

    body = json.dumps(
        {
            "event": event,
            "payload": {
                "refund": {"entity": {"id": refund_id, "payment_id": payment_id, "amount": amount}}
            },
        }
    ).encode()
    return body, {
        "X-Razorpay-Signature": webhook_signature(body),
        "X-Razorpay-Event-Id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "Content-Type": "application/json",
    }
