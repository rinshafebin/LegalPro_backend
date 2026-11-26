import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import UserRole
from tests.helpers import API, Actor, make_advocate, make_specialization, make_user

# ---------------------------------------------------------------- fixtures


@dataclass
class Directory:
    criminal_kochi: Actor
    family_delhi: Actor
    junior_kochi: Actor
    unverified: Actor
    disabled: Actor


@pytest.fixture
async def directory(db_session: AsyncSession) -> Directory:
    await make_specialization(db_session, "criminal-law", "Criminal Law")
    await make_specialization(db_session, "family-law", "Family Law")
    return Directory(
        criminal_kochi=await make_advocate(
            db_session,
            "anil@example.com",
            full_name="Anil Kumar",
            city="Kochi",
            languages=["english", "malayalam"],
            experience_years=15,
            specialization_slugs=["criminal-law"],
        ),
        family_delhi=await make_advocate(
            db_session,
            "priya@example.com",
            full_name="Priya Sharma",
            city="Delhi",
            languages=["english", "hindi"],
            experience_years=8,
            specialization_slugs=["family-law"],
        ),
        junior_kochi=await make_advocate(
            db_session,
            "rahul@example.com",
            full_name="Rahul Menon",
            city="kochi",
            languages=["malayalam"],
            experience_years=2,
            specialization_slugs=["criminal-law", "family-law"],
        ),
        unverified=await make_advocate(
            db_session, "new@example.com", full_name="Anil Unverified", verified=False
        ),
        disabled=await make_advocate(
            db_session, "gone@example.com", full_name="Anil Disabled", is_active=False
        ),
    )


async def _search(client: AsyncClient, **params: str | int) -> dict[str, object]:
    resp = await client.get(f"{API}/advocates", params=params)
    assert resp.status_code == 200, resp.text
    data: dict[str, object] = resp.json()
    return data


def _names(data: dict[str, object]) -> list[str]:
    items = data["items"]
    assert isinstance(items, list)
    return [i["full_name"] for i in items]


# ---------------------------------------------------------------- public search


async def test_search_lists_only_verified_active_advocates(
    client: AsyncClient, directory: Directory
) -> None:
    data = await _search(client)
    assert _names(data) == ["Anil Kumar", "Priya Sharma", "Rahul Menon"]  # experience desc
    assert data["total"] == 3


async def test_search_is_public_and_hides_private_fields(
    client: AsyncClient, directory: Directory
) -> None:
    data = await _search(client, q="anil")
    items = data["items"]
    assert isinstance(items, list)
    advocate = items[0]
    assert advocate["id"] == str(directory.criminal_kochi.id)  # User.id is the advocate id
    for private in ("phone", "email", "address_line1", "pincode"):
        assert private not in advocate
    assert advocate["specializations"] == [{"slug": "criminal-law", "name": "Criminal Law"}]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"q": "ANIL"}, ["Anil Kumar"]),
        ({"q": "men"}, ["Rahul Menon"]),
        ({"city": "KOCHI"}, ["Anil Kumar", "Rahul Menon"]),
        ({"language": "Hindi"}, ["Priya Sharma"]),
        ({"specialization": "family-law"}, ["Priya Sharma", "Rahul Menon"]),
        ({"min_experience": 5}, ["Anil Kumar", "Priya Sharma"]),
        ({"max_experience": 8}, ["Priya Sharma", "Rahul Menon"]),
        ({"city": "kochi", "specialization": "family-law"}, ["Rahul Menon"]),
        ({"q": "%n"}, []),  # LIKE wildcards are escaped (unescaped "%n" would match all)
    ],
)
async def test_search_filters(
    client: AsyncClient, directory: Directory, params: dict[str, str | int], expected: list[str]
) -> None:
    assert _names(await _search(client, **params)) == expected


async def test_search_pagination(client: AsyncClient, directory: Directory) -> None:
    first = await _search(client, page=1, page_size=2)
    second = await _search(client, page=2, page_size=2)
    assert _names(first) == ["Anil Kumar", "Priya Sharma"]
    assert _names(second) == ["Rahul Menon"]
    assert first["pages"] == second["pages"] == 2
    assert first["total"] == 3

    too_big = await client.get(f"{API}/advocates", params={"page_size": 1000})
    assert too_big.status_code == 422


async def test_public_detail_only_for_verified_active(
    client: AsyncClient, directory: Directory
) -> None:
    ok = await client.get(f"{API}/advocates/{directory.family_delhi.id}")
    assert ok.status_code == 200
    assert ok.json()["full_name"] == "Priya Sharma"

    for hidden in (directory.unverified, directory.disabled):
        assert (await client.get(f"{API}/advocates/{hidden.id}")).status_code == 404
    assert (await client.get(f"{API}/advocates/{uuid.uuid4()}")).status_code == 404


# ---------------------------------------------------------------- own profile


async def test_advocate_updates_own_profile(client: AsyncClient, directory: Directory) -> None:
    headers = directory.unverified.headers
    resp = await client.patch(
        f"{API}/advocates/me",
        headers=headers,
        json={
            "bio": "Criminal defence",
            "languages": ["English", " english ", "Tamil"],
            "specialization_slugs": ["criminal-law"],
            "consultation_fee": "1500.00",
            "enrollment_year": 2015,
            "experience_years": 10,
            "city": "Chennai",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["languages"] == ["english", "tamil"]
    assert body["specializations"] == [{"slug": "criminal-law", "name": "Criminal Law"}]
    assert body["consultation_fee"] == "1500.00"
    assert body["is_verified"] is False

    me = await client.get(f"{API}/advocates/me", headers=headers)
    assert me.json()["bio"] == "Criminal defence"


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"specialization_slugs": ["no-such-area"]}, 400),
        ({"enrollment_year": 2024, "experience_years": 20}, 400),
        ({"is_verified": True}, 422),
        ({"bar_council_id": "NEW/1"}, 422),
        ({"consultation_fee": "-1"}, 422),
        ({"enrollment_year": 1800}, 422),
    ],
)
async def test_advocate_profile_validation(
    client: AsyncClient, directory: Directory, payload: dict[str, object], status: int
) -> None:
    resp = await client.patch(
        f"{API}/advocates/me", headers=directory.unverified.headers, json=payload
    )
    assert resp.status_code == status, resp.text


# ---------------------------------------------------------------- specializations


async def test_specializations_public_list_admin_create(
    client: AsyncClient, db_session: AsyncSession, directory: Directory
) -> None:
    listed = [s["slug"] for s in (await client.get(f"{API}/specializations")).json()]
    assert {"criminal-law", "family-law"} <= set(listed)
    assert len(listed) == 14  # seeded by the migration

    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    body = {"slug": "maritime-law", "name": "Maritime Law"}
    assert (await client.post(f"{API}/specializations", json=body)).status_code == 401
    advocate = await client.post(
        f"{API}/specializations", json=body, headers=directory.criminal_kochi.headers
    )
    assert advocate.status_code == 403
    created = await client.post(f"{API}/specializations", json=body, headers=admin.headers)
    assert created.status_code == 201
    dup = await client.post(f"{API}/specializations", json=body, headers=admin.headers)
    assert dup.status_code == 409


# ---------------------------------------------------------------- admin


async def test_admin_lists_pending_advocates(
    client: AsyncClient, db_session: AsyncSession, directory: Directory
) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    resp = await client.get(f"{API}/admin/advocates", headers=admin.headers)
    assert resp.status_code == 200
    assert [a["email"] for a in resp.json()["items"]] == ["new@example.com"]

    everyone = await client.get(
        f"{API}/admin/advocates", params={"status": "all"}, headers=admin.headers
    )
    assert everyone.json()["total"] == 5


async def test_admin_unverify_hides_advocate_from_search(
    client: AsyncClient, db_session: AsyncSession, directory: Directory
) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")
    target = directory.family_delhi.id
    resp = await client.post(f"{API}/admin/advocates/{target}/unverify", headers=admin.headers)
    assert resp.status_code == 200
    assert "Priya Sharma" not in _names(await _search(client))


async def test_non_admins_cannot_use_admin_endpoints(
    client: AsyncClient, db_session: AsyncSession, directory: Directory
) -> None:
    client_user = await make_user(db_session, UserRole.CLIENT, "c@example.com")
    target = directory.unverified.id
    for actor in (client_user, directory.criminal_kochi):
        assert (
            await client.get(f"{API}/admin/advocates", headers=actor.headers)
        ).status_code == 403
        resp = await client.post(f"{API}/admin/advocates/{target}/verify", headers=actor.headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------- role checks


async def test_role_matrix_for_profiles(
    client: AsyncClient, db_session: AsyncSession, directory: Directory
) -> None:
    client_user = await make_user(db_session, UserRole.CLIENT, "c@example.com")
    advocate = directory.criminal_kochi

    assert (await client.get(f"{API}/advocates/me", headers=client_user.headers)).status_code == 403
    assert (await client.get(f"{API}/clients/me", headers=advocate.headers)).status_code == 403
    assert (await client.get(f"{API}/teams", headers=client_user.headers)).status_code == 403
    assert (await client.get(f"{API}/advocates/me")).status_code == 401


async def test_client_profile_get_and_update(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session, UserRole.CLIENT, "c@example.com", full_name="Asha")
    resp = await client.patch(
        f"{API}/clients/me",
        headers=user.headers,
        json={"city": "Kochi", "pincode": "682001", "address_line1": "MG Road"},
    )
    assert resp.status_code == 200
    me = (await client.get(f"{API}/clients/me", headers=user.headers)).json()
    assert me == {
        "full_name": "Asha",
        "phone": None,
        "address_line1": "MG Road",
        "address_line2": None,
        "city": "Kochi",
        "state": None,
        "pincode": "682001",
        "avatar_url": None,
    }


# ---------------------------------------------------------------- re-verification


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        ("/advocates/me", {"full_name": "Anil K. Renamed"}),
        ("/advocates/me", {"enrollment_year": 2010}),
        ("/users/me", {"full_name": "Anil K. Renamed"}),  # second path to the same field
    ],
)
async def test_identity_change_sends_verified_advocate_back_to_review(
    client: AsyncClient, directory: Directory, url: str, payload: dict[str, object]
) -> None:
    advocate = directory.criminal_kochi
    resp = await client.patch(f"{API}{url}", headers=advocate.headers, json=payload)
    assert resp.status_code == 200, resp.text

    me = (await client.get(f"{API}/advocates/me", headers=advocate.headers)).json()
    assert me["is_verified"] is False
    assert me["verified_at"] is None
    assert (await client.get(f"{API}/advocates/{advocate.id}")).status_code == 404
    assert "Anil Kumar" not in _names(await _search(client))


async def test_non_identity_changes_keep_verification(
    client: AsyncClient, directory: Directory
) -> None:
    advocate = directory.criminal_kochi
    resp = await client.patch(
        f"{API}/advocates/me",
        headers=advocate.headers,
        json={"bio": "Updated bio", "full_name": "Anil Kumar", "city": "Kochi"},  # name unchanged
    )
    assert resp.status_code == 200
    assert resp.json()["is_verified"] is True
