from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.deps import SessionDep
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import InvalidTokenError, TokenType, decode_token
from app.modules.users.models import User, UserRole

_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None:
        raise UnauthorizedError("Not authenticated", headers=_CHALLENGE)
    try:
        payload = decode_token(credentials.credentials, TokenType.ACCESS)
    except InvalidTokenError as exc:
        raise UnauthorizedError(str(exc), headers=_CHALLENGE) from exc

    user = await session.get(User, payload.sub)
    if user is None or not user.is_active:
        raise UnauthorizedError("User not found or inactive", headers=_CHALLENGE)
    if payload.ver != user.token_version:
        raise UnauthorizedError("Token has been revoked", headers=_CHALLENGE)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[[User], Awaitable[User]]:
    allowed = frozenset(roles)

    async def _dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise ForbiddenError("You do not have permission to perform this action")
        return user

    return _dependency
