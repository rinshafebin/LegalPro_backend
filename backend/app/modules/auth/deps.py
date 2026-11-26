from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.deps import SessionDep
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import InvalidTokenError, TokenPayload, TokenType, decode_token
from app.modules.users.models import User, UserRole

_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def access_token_payload(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> TokenPayload:
    if credentials is None:
        raise UnauthorizedError("Not authenticated", headers=_CHALLENGE)
    try:
        return decode_token(credentials.credentials, TokenType.ACCESS)
    except InvalidTokenError as exc:
        raise UnauthorizedError(str(exc), headers=_CHALLENGE) from exc


TokenDep = Annotated[TokenPayload, Depends(access_token_payload)]


async def get_current_user(session: SessionDep, payload: TokenDep) -> User:
    user = await session.get(User, payload.sub)
    if user is None or not user.is_active:
        raise UnauthorizedError("User not found or inactive", headers=_CHALLENGE)
    if payload.ver != user.token_version:
        raise UnauthorizedError("Token has been revoked", headers=_CHALLENGE)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[[User, TokenPayload], Awaitable[User]]:
    """Role check. Admins additionally need MFA on the account *and* an MFA-verified session."""
    allowed = frozenset(roles)

    async def _dependency(user: CurrentUser, payload: TokenDep) -> User:
        if user.role not in allowed:
            raise ForbiddenError("You do not have permission to perform this action")
        if user.role is UserRole.ADMIN and not (user.mfa_enabled and payload.mfa):
            raise ForbiddenError(
                "Admin access requires two-factor authentication: enable it with "
                "/auth/mfa/setup, then log in again",
                code="admin_mfa_required",
            )
        return user

    return _dependency


require_admin = require_roles(UserRole.ADMIN)
AdminUser = Annotated[User, Depends(require_admin)]
