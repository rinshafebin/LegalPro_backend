from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.modules.clients.models import ClientProfile
from app.modules.clients.schemas import ClientProfileUpdateIn
from app.modules.users.models import User


async def get_my_profile(session: AsyncSession, user: User) -> ClientProfile:
    profile = await session.get(ClientProfile, user.id)
    if profile is None:
        raise NotFoundError("Client profile not found")
    return profile


async def update_my_profile(
    session: AsyncSession, user: User, data: ClientProfileUpdateIn
) -> ClientProfile:
    profile = await get_my_profile(session, user)
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "full_name" and value is None:
            continue  # NOT NULL column
        setattr(profile, field, value)
    await session.commit()
    return profile
