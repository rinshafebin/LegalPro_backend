"""Shared test helpers for creating users and logging in through the API."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
from httpx import AsyncClient

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
