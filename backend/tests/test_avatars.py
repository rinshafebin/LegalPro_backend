"""Profile images: PNG/JPEG only (by content), 2 MB, re-encoded without metadata, signed URLs."""

import io

import pytest
from httpx import AsyncClient, Response
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.users.models import UserRole
from tests.conftest import FakeStorage
from tests.helpers import API, Actor, make_advocate, make_user

URL = f"{API}/users/me/avatar"


def _image(fmt: str, size: tuple[int, int] = (32, 32), exif: bool = False) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", size, (200, 30, 30))
    kwargs = {}
    if exif:
        data = Image.Exif()
        data[0x010F] = "SpyPhone"  # Make
        data[0x8825] = {2: (10.0, 1.0, 0.0)}  # GPSInfo: GPSLatitude
        kwargs["exif"] = data.tobytes()
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


async def _upload(
    client: AsyncClient, actor: Actor, data: bytes, name: str = "me.png", ctype: str = "image/png"
) -> Response:
    return await client.put(URL, headers=actor.headers, files={"file": (name, data, ctype)})


async def test_client_avatar_upload_replace_and_remove(
    client: AsyncClient, db_session: AsyncSession, storage: FakeStorage
) -> None:
    me = await make_user(db_session, UserRole.CLIENT, "pic@example.com")
    resp = await _upload(client, me, _image("PNG"))
    assert resp.status_code == 200, resp.text
    assert "sig=" in resp.json()["avatar_url"]
    [first_key] = storage.objects
    assert first_key.startswith(f"avatars/{me.id}/")
    assert first_key.endswith(".png")

    profile = (await client.get(f"{API}/clients/me", headers=me.headers)).json()
    assert first_key in profile["avatar_url"]

    await _upload(client, me, _image("JPEG"), "me.jpg", "image/jpeg")
    [second_key] = storage.objects  # the old object was removed
    assert second_key != first_key
    assert second_key.endswith(".jpg")

    assert (await client.delete(URL, headers=me.headers)).status_code == 204
    assert storage.objects == {}
    assert (await client.get(f"{API}/clients/me", headers=me.headers)).json()["avatar_url"] is None


async def test_metadata_is_stripped(
    client: AsyncClient, db_session: AsyncSession, storage: FakeStorage
) -> None:
    me = await make_user(db_session, UserRole.CLIENT, "gps@example.com")
    original = _image("JPEG", exif=True)
    assert Image.open(io.BytesIO(original)).getexif().get(0x010F) == "SpyPhone"
    await _upload(client, me, original, "me.jpg", "image/jpeg")
    [(stored, content_type)] = storage.objects.values()
    assert content_type == "image/jpeg"
    reopened = Image.open(io.BytesIO(stored))
    assert dict(reopened.getexif()) == {}
    assert reopened.size == (32, 32)


async def test_advocate_avatar_shows_in_public_search(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    adv = await make_advocate(db_session, "face@example.com", full_name="Adv. Face")
    await make_advocate(db_session, "noface@example.com", full_name="Adv. Plain")
    await _upload(client, adv, _image("PNG"))

    results = (await client.get(f"{API}/advocates")).json()["items"]
    urls = {r["full_name"]: r["avatar_url"] for r in results}
    assert "sig=" in urls["Adv. Face"]
    assert f"avatars/{adv.id}/" in urls["Adv. Face"]
    assert urls["Adv. Plain"] is None
    detail = (await client.get(f"{API}/advocates/{adv.id}")).json()
    assert "sig=" in detail["avatar_url"]
    me = (await client.get(f"{API}/advocates/me", headers=adv.headers)).json()
    assert "sig=" in me["avatar_url"]


@pytest.mark.parametrize(
    ("name", "data", "status"),
    [
        ("doc.pdf", b"%PDF-1.7\n%%EOF", 415),  # allowed for cases, not for avatars
        ("me.png", b"\x89PNG\r\n\x1a\n" + b"garbage" * 10, 415),  # PNG header, not an image
        ("me.gif", b"GIF89a" + b"\x00" * 20, 415),
        ("me.png", b"", 400),
    ],
)
async def test_rejected_uploads(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: FakeStorage,
    name: str,
    data: bytes,
    status: int,
) -> None:
    me = await make_user(db_session, UserRole.CLIENT, "bad@example.com")
    resp = await _upload(client, me, data, name)
    assert resp.status_code == status
    assert storage.objects == {}


async def test_size_and_dimension_limits(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    me = await make_user(db_session, UserRole.CLIENT, "big@example.com")
    too_big = _image("PNG") + b"\x00" * (2 * 1024 * 1024)
    assert (await _upload(client, me, too_big)).status_code == 413
    monkeypatch.setattr(get_settings(), "avatar_max_pixels", 16)
    wide = _image("PNG", size=(17, 4))
    assert (await _upload(client, me, wide)).status_code == 415
    assert storage.objects == {}


async def test_admins_and_anonymous_cant_upload(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "root@example.com")
    assert (await _upload(client, admin, _image("PNG"))).status_code == 403
    anon = await client.put(URL, files={"file": ("a.png", _image("PNG"), "image/png")})
    assert anon.status_code == 401
