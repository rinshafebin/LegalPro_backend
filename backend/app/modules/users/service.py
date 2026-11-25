from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advocates.models import AdvocateProfile
from app.modules.clients.models import ClientProfile
from app.modules.users.models import User, UserRole
from app.modules.users.schemas import MeUpdateIn, ProfileSummary, UserOut


async def get_profile(session: AsyncSession, user: User) -> ClientProfile | AdvocateProfile | None:
    if user.role is UserRole.CLIENT:
        return await session.get(ClientProfile, user.id)
    if user.role is UserRole.ADVOCATE:
        return await session.get(AdvocateProfile, user.id)
    return None


async def to_user_out(session: AsyncSession, user: User) -> UserOut:
    profile = await get_profile(session, user)
    summary: ProfileSummary | None = None
    if isinstance(profile, AdvocateProfile):
        summary = ProfileSummary(
            full_name=profile.full_name,
            phone=profile.phone,
            bar_council_id=profile.bar_council_id,
            is_verified=profile.is_verified,
        )
    elif isinstance(profile, ClientProfile):
        summary = ProfileSummary(full_name=profile.full_name, phone=profile.phone)

    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        email_verified=user.email_verified,
        mfa_enabled=user.mfa_enabled,
        created_at=user.created_at,
        profile=summary,
    )


async def update_me(session: AsyncSession, user: User, data: MeUpdateIn) -> UserOut:
    profile = await get_profile(session, user)
    changes = data.model_dump(exclude_unset=True)
    if isinstance(profile, AdvocateProfile):
        from app.modules.advocates.service import apply_identity_changes  # avoid import cycle

        apply_identity_changes(profile, changes)
    if profile is not None:
        for field, value in changes.items():
            if field == "full_name" and value is None:
                continue  # full_name is required on profiles
            setattr(profile, field, value)
        await session.commit()
    return await to_user_out(session, user)
