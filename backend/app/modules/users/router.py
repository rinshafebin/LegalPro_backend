from typing import Annotated

from fastapi import APIRouter, File, UploadFile, status
from pydantic import BaseModel

from app.core.deps import SessionDep
from app.modules.auth.deps import CurrentUser
from app.modules.users import avatars, service
from app.modules.users.schemas import MeUpdateIn, UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def get_me(user: CurrentUser, session: SessionDep) -> UserOut:
    return await service.to_user_out(session, user)


@router.patch("/me", response_model=UserOut)
async def update_me(data: MeUpdateIn, user: CurrentUser, session: SessionDep) -> UserOut:
    return await service.update_me(session, user, data)


class AvatarOut(BaseModel):
    avatar_url: str | None


@router.put(
    "/me/avatar",
    response_model=AvatarOut,
    description="Clients and advocates. Multipart `file`: PNG or JPEG (checked by content), "
    "max `AVATAR_MAX_MB`. Re-encoded without metadata. Replaces any previous image.",
)
async def set_avatar(
    user: CurrentUser, session: SessionDep, file: Annotated[UploadFile, File()]
) -> AvatarOut:
    return AvatarOut(avatar_url=await avatars.set_avatar(session, user, file))


@router.delete("/me/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def remove_avatar(user: CurrentUser, session: SessionDep) -> None:
    await avatars.remove_avatar(session, user)
