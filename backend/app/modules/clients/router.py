from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import SessionDep
from app.modules.auth.deps import require_roles
from app.modules.clients import service
from app.modules.clients.models import ClientProfile
from app.modules.clients.schemas import ClientProfileOut, ClientProfileUpdateIn
from app.modules.users.avatars import avatar_url
from app.modules.users.models import User, UserRole

ClientUser = Annotated[User, Depends(require_roles(UserRole.CLIENT))]

router = APIRouter(prefix="/clients", tags=["clients"])


async def _out(profile: ClientProfile) -> ClientProfileOut:
    out = ClientProfileOut.model_validate(profile)
    out.avatar_url = await avatar_url(profile.avatar_key)
    return out


@router.get("/me", response_model=ClientProfileOut)
async def get_my_profile(user: ClientUser, session: SessionDep) -> ClientProfileOut:
    return await _out(await service.get_my_profile(session, user))


@router.patch("/me", response_model=ClientProfileOut)
async def update_my_profile(
    data: ClientProfileUpdateIn, user: ClientUser, session: SessionDep
) -> ClientProfileOut:
    return await _out(await service.update_my_profile(session, user, data))
