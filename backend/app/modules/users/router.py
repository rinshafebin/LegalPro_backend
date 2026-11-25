from fastapi import APIRouter

from app.core.deps import SessionDep
from app.modules.auth.deps import CurrentUser
from app.modules.users import service
from app.modules.users.schemas import MeUpdateIn, UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def get_me(user: CurrentUser, session: SessionDep) -> UserOut:
    return await service.to_user_out(session, user)


@router.patch("/me", response_model=UserOut)
async def update_me(data: MeUpdateIn, user: CurrentUser, session: SessionDep) -> UserOut:
    return await service.update_me(session, user, data)
