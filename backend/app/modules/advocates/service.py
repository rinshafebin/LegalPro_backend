import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.email import enqueue_email
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.core.pagination import PageParams, paginate
from app.modules.advocates.models import (
    AdvocateProfile,
    AdvocateTeam,
    Specialization,
    TeamMember,
)
from app.modules.advocates.schemas import (
    AdminAdvocateOut,
    AdvocateMeUpdateIn,
    AdvocateSearchParams,
    SpecializationCreateIn,
    TeamCreateIn,
    TeamMemberOut,
    TeamOut,
)
from app.modules.auth import emails
from app.modules.users.models import User

logger = get_logger(__name__)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- specializations


async def list_specializations(session: AsyncSession) -> list[Specialization]:
    return list((await session.scalars(select(Specialization).order_by(Specialization.name))).all())


async def create_specialization(
    session: AsyncSession, data: SpecializationCreateIn
) -> Specialization:
    spec = Specialization(slug=data.slug, name=data.name)
    session.add(spec)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("A specialization with this slug or name already exists") from exc
    return spec


async def _specializations_by_slug(session: AsyncSession, slugs: list[str]) -> list[Specialization]:
    wanted = list(dict.fromkeys(slugs))
    found = (
        await session.scalars(select(Specialization).where(Specialization.slug.in_(wanted)))
    ).all()
    missing = sorted(set(wanted) - {s.slug for s in found})
    if missing:
        raise BadRequestError("Unknown specializations", details={"unknown": missing})
    return list(found)


# ---------------------------------------------------------------- own profile


async def get_my_profile(session: AsyncSession, user: User) -> AdvocateProfile:
    profile = await session.scalar(
        select(AdvocateProfile)
        .where(AdvocateProfile.user_id == user.id)
        .options(selectinload(AdvocateProfile.specializations))
    )
    if profile is None:
        raise NotFoundError("Advocate profile not found")
    return profile


async def update_my_profile(
    session: AsyncSession, user: User, data: AdvocateMeUpdateIn
) -> AdvocateProfile:
    profile = await get_my_profile(session, user)
    changes = data.model_dump(exclude_unset=True)

    slugs = changes.pop("specialization_slugs", None)
    if slugs is not None:
        profile.specializations = await _specializations_by_slug(session, slugs)

    for field, value in changes.items():
        if field in {"full_name", "experience_years", "languages"} and value is None:
            continue  # NOT NULL columns: ignore explicit nulls
        setattr(profile, field, value)

    if profile.enrollment_year is not None:
        max_years = datetime.now(UTC).year - profile.enrollment_year + 1
        if profile.experience_years > max_years:
            raise BadRequestError("Experience exceeds the years since enrollment")

    await session.commit()
    return profile


# ---------------------------------------------------------------- public search


def _public_advocates() -> Select[AdvocateProfile]:
    return (
        select(AdvocateProfile)
        .join(User, User.id == AdvocateProfile.user_id)
        .where(AdvocateProfile.is_verified.is_(True), User.is_active.is_(True))
    )


async def search_advocates(
    session: AsyncSession, params: AdvocateSearchParams, page: PageParams
) -> tuple[list[AdvocateProfile], int]:
    stmt = _public_advocates()
    if params.q:
        stmt = stmt.where(
            AdvocateProfile.full_name.ilike(f"%{_escape_like(params.q)}%", escape="\\")
        )
    if params.city:
        stmt = stmt.where(func.lower(AdvocateProfile.city) == params.city.strip().lower())
    if params.language:
        stmt = stmt.where(AdvocateProfile.languages.contains([params.language.strip().lower()]))
    if params.specialization:
        stmt = stmt.where(
            AdvocateProfile.specializations.any(Specialization.slug == params.specialization)
        )
    if params.min_experience is not None:
        stmt = stmt.where(AdvocateProfile.experience_years >= params.min_experience)
    if params.max_experience is not None:
        stmt = stmt.where(AdvocateProfile.experience_years <= params.max_experience)

    stmt = stmt.options(selectinload(AdvocateProfile.specializations)).order_by(
        AdvocateProfile.experience_years.desc(),
        AdvocateProfile.full_name,
        AdvocateProfile.user_id,  # stable order across pages
    )
    return await paginate(session, stmt, page)


async def get_public_advocate(session: AsyncSession, advocate_id: uuid.UUID) -> AdvocateProfile:
    profile = await session.scalar(
        _public_advocates()
        .where(AdvocateProfile.user_id == advocate_id)
        .options(selectinload(AdvocateProfile.specializations))
    )
    if profile is None:
        raise NotFoundError("Advocate not found")
    return profile


# ---------------------------------------------------------------- admin verification


async def admin_list_advocates(
    session: AsyncSession, status: Literal["pending", "verified", "all"], page: PageParams
) -> tuple[list[AdminAdvocateOut], int]:
    stmt = select(AdvocateProfile).options(joinedload(AdvocateProfile.user))
    if status == "pending":
        stmt = stmt.where(AdvocateProfile.is_verified.is_(False))
    elif status == "verified":
        stmt = stmt.where(AdvocateProfile.is_verified.is_(True))
    stmt = stmt.order_by(AdvocateProfile.created_at, AdvocateProfile.user_id)
    rows, total = await paginate(session, stmt, page)
    return [_admin_out(p) for p in rows], total


def _admin_out(profile: AdvocateProfile) -> AdminAdvocateOut:
    return AdminAdvocateOut(
        id=profile.user_id,
        email=profile.user.email,
        email_verified=profile.user.email_verified,
        full_name=profile.full_name,
        bar_council_id=profile.bar_council_id,
        is_verified=profile.is_verified,
        verified_at=profile.verified_at,
        created_at=profile.created_at,
    )


async def _get_profile_with_user(session: AsyncSession, advocate_id: uuid.UUID) -> AdvocateProfile:
    profile = await session.scalar(
        select(AdvocateProfile)
        .where(AdvocateProfile.user_id == advocate_id)
        .options(joinedload(AdvocateProfile.user))
    )
    if profile is None:
        raise NotFoundError("Advocate not found")
    return profile


async def verify_advocate(
    session: AsyncSession, admin: User, advocate_id: uuid.UUID
) -> AdminAdvocateOut:
    profile = await _get_profile_with_user(session, advocate_id)
    if not profile.user.is_active:
        raise ConflictError("Cannot verify a disabled account")
    if not profile.user.email_verified:
        raise ConflictError(
            "The advocate must verify their email address first", code="email_not_verified"
        )
    if not profile.is_verified:
        profile.is_verified = True
        profile.verified_at = datetime.now(UTC)
        profile.verified_by_id = admin.id
        await session.commit()
        subject, body = emails.advocate_verified(profile.full_name)
        await enqueue_email(profile.user.email, subject, body)
        logger.info("advocate_verified", advocate_id=str(advocate_id), admin_id=str(admin.id))
    return _admin_out(profile)


async def unverify_advocate(
    session: AsyncSession, admin: User, advocate_id: uuid.UUID
) -> AdminAdvocateOut:
    profile = await _get_profile_with_user(session, advocate_id)
    if profile.is_verified:
        profile.is_verified = False
        profile.verified_at = None
        profile.verified_by_id = None
        await session.commit()
        logger.info("advocate_unverified", advocate_id=str(advocate_id), admin_id=str(admin.id))
    return _admin_out(profile)


# ---------------------------------------------------------------- teams


async def _require_verified_advocate(session: AsyncSession, user: User) -> None:
    profile = await session.get(AdvocateProfile, user.id)
    if profile is None or not profile.is_verified:
        raise ForbiddenError("Only verified advocates can manage teams")


async def _check_eligible_members(
    session: AsyncSession, member_ids: list[uuid.UUID], lead_id: uuid.UUID
) -> list[uuid.UUID]:
    ids = list(dict.fromkeys(member_ids))
    if lead_id in ids:
        raise BadRequestError("The team lead is already part of the team")
    if not ids:
        return []
    eligible = set(
        (
            await session.scalars(
                select(AdvocateProfile.user_id)
                .join(User, User.id == AdvocateProfile.user_id)
                .where(
                    AdvocateProfile.user_id.in_(ids),
                    AdvocateProfile.is_verified.is_(True),
                    User.is_active.is_(True),
                )
            )
        ).all()
    )
    invalid = [str(i) for i in ids if i not in eligible]
    if invalid:
        raise BadRequestError(
            "Team members must be verified advocates", details={"invalid_ids": invalid}
        )
    return ids


async def _load_team(session: AsyncSession, team_id: uuid.UUID) -> AdvocateTeam | None:
    team: AdvocateTeam | None = await session.scalar(
        select(AdvocateTeam)
        .where(AdvocateTeam.id == team_id)
        .options(selectinload(AdvocateTeam.members))
    )
    return team


def _is_member(team: AdvocateTeam, user_id: uuid.UUID) -> bool:
    return any(m.advocate_id == user_id for m in team.members)


async def get_visible_team(session: AsyncSession, user: User, team_id: uuid.UUID) -> AdvocateTeam:
    """The lead and members can see a team. To anyone else it doesn't exist (404, not 403)."""
    team = await _load_team(session, team_id)
    if team is None or (team.lead_id != user.id and not _is_member(team, user.id)):
        raise NotFoundError("Team not found")
    return team


async def _get_led_team(session: AsyncSession, user: User, team_id: uuid.UUID) -> AdvocateTeam:
    team = await get_visible_team(session, user, team_id)
    if team.lead_id != user.id:
        raise ForbiddenError("Only the team lead can do this")
    return team


async def list_my_teams(session: AsyncSession, user: User) -> list[AdvocateTeam]:
    member_of = select(TeamMember.team_id).where(TeamMember.advocate_id == user.id)
    stmt = (
        select(AdvocateTeam)
        .where(or_(AdvocateTeam.lead_id == user.id, AdvocateTeam.id.in_(member_of)))
        .options(selectinload(AdvocateTeam.members))
        .order_by(AdvocateTeam.created_at)
    )
    return list((await session.scalars(stmt)).all())


async def create_team(session: AsyncSession, lead: User, data: TeamCreateIn) -> AdvocateTeam:
    await _require_verified_advocate(session, lead)
    member_ids = await _check_eligible_members(session, data.member_ids, lead.id)
    team = AdvocateTeam(
        name=data.name.strip(),
        lead_id=lead.id,
        members=[TeamMember(advocate_id=i) for i in member_ids],
    )
    session.add(team)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("You already lead a team with this name") from exc
    return await get_visible_team(session, lead, team.id)


async def rename_team(
    session: AsyncSession, user: User, team_id: uuid.UUID, name: str
) -> AdvocateTeam:
    team = await _get_led_team(session, user, team_id)
    team.name = name.strip()
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("You already lead a team with this name") from exc
    return team


async def delete_team(session: AsyncSession, user: User, team_id: uuid.UUID) -> None:
    team = await _get_led_team(session, user, team_id)
    await session.delete(team)
    await session.commit()


async def add_member(
    session: AsyncSession, user: User, team_id: uuid.UUID, advocate_id: uuid.UUID
) -> AdvocateTeam:
    team = await _get_led_team(session, user, team_id)
    if _is_member(team, advocate_id):
        raise ConflictError("Advocate is already a member of this team")
    await _check_eligible_members(session, [advocate_id], team.lead_id)
    team.members.append(TeamMember(advocate_id=advocate_id))
    await session.commit()
    return await get_visible_team(session, user, team_id)


async def remove_member(
    session: AsyncSession, user: User, team_id: uuid.UUID, advocate_id: uuid.UUID
) -> None:
    """The lead can remove anyone; a member can remove only themselves (leave the team)."""
    team = await get_visible_team(session, user, team_id)
    if user.id not in (team.lead_id, advocate_id):
        raise ForbiddenError("Only the team lead can remove other members")
    member = next((m for m in team.members if m.advocate_id == advocate_id), None)
    if member is None:
        raise NotFoundError("Advocate is not a member of this team")
    team.members.remove(member)
    await session.commit()


async def to_team_out(session: AsyncSession, team: AdvocateTeam) -> TeamOut:
    ids = [team.lead_id, *(m.advocate_id for m in team.members)]
    result = await session.execute(
        select(AdvocateProfile.user_id, AdvocateProfile.full_name).where(
            AdvocateProfile.user_id.in_(ids)
        )
    )
    names = dict(result.all())
    return TeamOut(
        id=team.id,
        name=team.name,
        lead=TeamMemberOut(id=team.lead_id, full_name=names.get(team.lead_id, "")),
        members=[
            TeamMemberOut(
                id=m.advocate_id, full_name=names.get(m.advocate_id, ""), joined_at=m.joined_at
            )
            for m in team.members
        ],
        created_at=team.created_at,
    )
