import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import API, Actor, make_advocate


@dataclass
class Firm:
    lead: Actor
    member: Actor
    outsider: Actor
    unverified: Actor


@pytest.fixture
async def firm(db_session: AsyncSession) -> Firm:
    return Firm(
        lead=await make_advocate(db_session, "lead@example.com", full_name="Lead Advocate"),
        member=await make_advocate(db_session, "member@example.com", full_name="Member Advocate"),
        outsider=await make_advocate(db_session, "out@example.com", full_name="Outside Advocate"),
        unverified=await make_advocate(db_session, "unv@example.com", verified=False),
    )


async def _create_team(client: AsyncClient, firm: Firm, name: str = "Litigation") -> str:
    resp = await client.post(
        f"{API}/teams",
        headers=firm.lead.headers,
        json={"name": name, "member_ids": [str(firm.member.id)]},
    )
    assert resp.status_code == 201, resp.text
    team_id: str = resp.json()["id"]
    return team_id


async def test_create_team_with_members(client: AsyncClient, firm: Firm) -> None:
    resp = await client.post(
        f"{API}/teams",
        headers=firm.lead.headers,
        json={"name": "Litigation", "member_ids": [str(firm.member.id)]},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["lead"] == {
        "id": str(firm.lead.id),
        "full_name": "Lead Advocate",
        "joined_at": None,
    }
    assert [(m["id"], m["full_name"]) for m in body["members"]] == [
        (str(firm.member.id), "Member Advocate")
    ]


async def test_unverified_advocate_cannot_create_team(client: AsyncClient, firm: Firm) -> None:
    resp = await client.post(f"{API}/teams", headers=firm.unverified.headers, json={"name": "Solo"})
    assert resp.status_code == 403


@pytest.mark.parametrize("bad_member", ["unverified", "lead", "random"])
async def test_team_members_must_be_other_verified_advocates(
    client: AsyncClient, firm: Firm, bad_member: str
) -> None:
    member_id = uuid.uuid4() if bad_member == "random" else getattr(firm, bad_member).id
    resp = await client.post(
        f"{API}/teams",
        headers=firm.lead.headers,
        json={"name": "Bad", "member_ids": [str(member_id)]},
    )
    assert resp.status_code == 400


async def test_team_name_unique_per_lead(client: AsyncClient, firm: Firm) -> None:
    await _create_team(client, firm, "Litigation")
    resp = await client.post(f"{API}/teams", headers=firm.lead.headers, json={"name": "Litigation"})
    assert resp.status_code == 409


async def test_team_visibility(client: AsyncClient, firm: Firm) -> None:
    team_id = await _create_team(client, firm)
    url = f"{API}/teams/{team_id}"

    assert (await client.get(url, headers=firm.lead.headers)).status_code == 200
    assert (await client.get(url, headers=firm.member.headers)).status_code == 200
    # Outsiders can't even tell the team exists.
    assert (await client.get(url, headers=firm.outsider.headers)).status_code == 404

    member_teams = await client.get(f"{API}/teams", headers=firm.member.headers)
    assert [t["id"] for t in member_teams.json()] == [team_id]
    outsider_teams = await client.get(f"{API}/teams", headers=firm.outsider.headers)
    assert outsider_teams.json() == []


async def test_only_lead_manages_members(client: AsyncClient, firm: Firm) -> None:
    team_id = await _create_team(client, firm)
    members_url = f"{API}/teams/{team_id}/members"
    body = {"advocate_id": str(firm.outsider.id)}

    assert (
        await client.post(members_url, headers=firm.member.headers, json=body)
    ).status_code == 403
    assert (
        await client.post(members_url, headers=firm.outsider.headers, json=body)
    ).status_code == 404

    added = await client.post(members_url, headers=firm.lead.headers, json=body)
    assert added.status_code == 201
    assert len(added.json()["members"]) == 2
    dup = await client.post(members_url, headers=firm.lead.headers, json=body)
    assert dup.status_code == 409

    # A member can't remove someone else...
    other = f"{members_url}/{firm.outsider.id}"
    assert (await client.delete(other, headers=firm.member.headers)).status_code == 403
    # ...but can leave.
    leave = f"{members_url}/{firm.member.id}"
    assert (await client.delete(leave, headers=firm.member.headers)).status_code == 204
    assert (
        await client.get(f"{API}/teams/{team_id}", headers=firm.member.headers)
    ).status_code == 404


async def test_lead_renames_and_deletes_team(client: AsyncClient, firm: Firm) -> None:
    team_id = await _create_team(client, firm)
    url = f"{API}/teams/{team_id}"

    assert (
        await client.patch(url, headers=firm.member.headers, json={"name": "Hijack"})
    ).status_code == 403
    renamed = await client.patch(url, headers=firm.lead.headers, json={"name": "Appeals"})
    assert renamed.json()["name"] == "Appeals"

    assert (await client.delete(url, headers=firm.member.headers)).status_code == 403
    assert (await client.delete(url, headers=firm.lead.headers)).status_code == 204
    assert (await client.get(url, headers=firm.lead.headers)).status_code == 404
