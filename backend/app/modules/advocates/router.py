import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import SessionDep
from app.core.pagination import Page, PageDep, make_page
from app.modules.advocates import dashboard, service
from app.modules.advocates.models import AdvocateProfile
from app.modules.advocates.schemas import (
    AdminAdvocateOut,
    AdvocateMeOut,
    AdvocateMeUpdateIn,
    AdvocatePublicOut,
    AdvocateSearchParams,
    SpecializationCreateIn,
    SpecializationOut,
    TeamCreateIn,
    TeamMemberAddIn,
    TeamOut,
    TeamUpdateIn,
)
from app.modules.auth.deps import require_roles
from app.modules.users.avatars import avatar_url
from app.modules.users.models import User, UserRole

AdvocateUser = Annotated[User, Depends(require_roles(UserRole.ADVOCATE))]
AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]

router = APIRouter(prefix="/advocates", tags=["advocates"])
specializations_router = APIRouter(prefix="/specializations", tags=["advocates"])
teams_router = APIRouter(prefix="/teams", tags=["teams"])
admin_router = APIRouter(prefix="/admin/advocates", tags=["admin"])


async def _public(profile: AdvocateProfile) -> AdvocatePublicOut:
    out = AdvocatePublicOut.model_validate(profile)
    out.avatar_url = await avatar_url(profile.avatar_key)
    return out


async def _me(profile: AdvocateProfile) -> AdvocateMeOut:
    out = AdvocateMeOut.model_validate(profile)
    out.avatar_url = await avatar_url(profile.avatar_key)
    return out


# ---------------------------------------------------------------- own profile
# Declared before "/{advocate_id}" so "/me" isn't parsed as an id.


@router.get("/me", response_model=AdvocateMeOut)
async def get_my_profile(user: AdvocateUser, session: SessionDep) -> AdvocateMeOut:
    return await _me(await service.get_my_profile(session, user))


@router.get(
    "/me/dashboard",
    response_model=dashboard.DashboardOut,
    description="Cases by status, hearings in the next 7 days, upcoming bookings, "
    "pending payments and refunds in progress.",
)
async def my_dashboard(user: AdvocateUser, session: SessionDep) -> dashboard.DashboardOut:
    return await dashboard.build(session, user)


@router.patch("/me", response_model=AdvocateMeOut)
async def update_my_profile(
    data: AdvocateMeUpdateIn, user: AdvocateUser, session: SessionDep
) -> AdvocateMeOut:
    return await _me(await service.update_my_profile(session, user, data))


# ---------------------------------------------------------------- public


@router.get(
    "",
    response_model=Page[AdvocatePublicOut],
    summary="Search verified advocates",
    description="Public. Only verified advocates with active accounts are listed.",
)
async def search_advocates(
    session: SessionDep,
    page: PageDep,
    params: Annotated[AdvocateSearchParams, Query()],
) -> Page[AdvocatePublicOut]:
    rows, total = await service.search_advocates(session, params, page)
    return make_page([await _public(r) for r in rows], total, page)


@router.get("/{advocate_id}", response_model=AdvocatePublicOut)
async def get_advocate(advocate_id: uuid.UUID, session: SessionDep) -> AdvocatePublicOut:
    return await _public(await service.get_public_advocate(session, advocate_id))


# ---------------------------------------------------------------- specializations


@specializations_router.get("", response_model=list[SpecializationOut])
async def list_specializations(session: SessionDep) -> list[SpecializationOut]:
    return [
        SpecializationOut.model_validate(s) for s in await service.list_specializations(session)
    ]


@specializations_router.post(
    "", response_model=SpecializationOut, status_code=status.HTTP_201_CREATED
)
async def create_specialization(
    data: SpecializationCreateIn, _: AdminUser, session: SessionDep
) -> SpecializationOut:
    return SpecializationOut.model_validate(await service.create_specialization(session, data))


# ---------------------------------------------------------------- teams


@teams_router.get("", response_model=list[TeamOut], summary="Teams I lead or belong to")
async def list_teams(user: AdvocateUser, session: SessionDep) -> list[TeamOut]:
    return [
        await service.to_team_out(session, t) for t in await service.list_my_teams(session, user)
    ]


@teams_router.post("", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
async def create_team(data: TeamCreateIn, user: AdvocateUser, session: SessionDep) -> TeamOut:
    return await service.to_team_out(session, await service.create_team(session, user, data))


@teams_router.get("/{team_id}", response_model=TeamOut)
async def get_team(team_id: uuid.UUID, user: AdvocateUser, session: SessionDep) -> TeamOut:
    return await service.to_team_out(
        session, await service.get_visible_team(session, user, team_id)
    )


@teams_router.patch("/{team_id}", response_model=TeamOut)
async def rename_team(
    team_id: uuid.UUID, data: TeamUpdateIn, user: AdvocateUser, session: SessionDep
) -> TeamOut:
    team = await service.rename_team(session, user, team_id, data.name)
    return await service.to_team_out(session, team)


@teams_router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(team_id: uuid.UUID, user: AdvocateUser, session: SessionDep) -> None:
    await service.delete_team(session, user, team_id)


@teams_router.post(
    "/{team_id}/members", response_model=TeamOut, status_code=status.HTTP_201_CREATED
)
async def add_team_member(
    team_id: uuid.UUID, data: TeamMemberAddIn, user: AdvocateUser, session: SessionDep
) -> TeamOut:
    team = await service.add_member(session, user, team_id, data.advocate_id)
    return await service.to_team_out(session, team)


@teams_router.delete(
    "/{team_id}/members/{advocate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    description="The lead can remove any member; a member can remove themselves (leave).",
)
async def remove_team_member(
    team_id: uuid.UUID, advocate_id: uuid.UUID, user: AdvocateUser, session: SessionDep
) -> None:
    await service.remove_member(session, user, team_id, advocate_id)


# ---------------------------------------------------------------- admin


@admin_router.get("", response_model=Page[AdminAdvocateOut])
async def admin_list_advocates(
    _: AdminUser,
    session: SessionDep,
    page: PageDep,
    status_filter: Annotated[
        Literal["pending", "verified", "all"], Query(alias="status")
    ] = "pending",
) -> Page[AdminAdvocateOut]:
    rows, total = await service.admin_list_advocates(session, status_filter, page)
    return make_page(rows, total, page)


@admin_router.post(
    "/{advocate_id}/verify",
    response_model=AdminAdvocateOut,
    description="Requires the advocate to have verified their email address.",
)
async def verify_advocate(
    advocate_id: uuid.UUID, admin: AdminUser, session: SessionDep
) -> AdminAdvocateOut:
    return await service.verify_advocate(session, admin, advocate_id)


@admin_router.post("/{advocate_id}/unverify", response_model=AdminAdvocateOut)
async def unverify_advocate(
    advocate_id: uuid.UUID, admin: AdminUser, session: SessionDep
) -> AdminAdvocateOut:
    return await service.unverify_advocate(session, admin, advocate_id)
