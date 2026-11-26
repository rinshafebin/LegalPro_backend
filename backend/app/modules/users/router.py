from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.deps import SessionDep
from app.core.rate_limit import hit
from app.modules.audit.service import AuditDep
from app.modules.auth.deps import CurrentUser, TokenDep
from app.modules.users import account, avatars, service
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


class DeletionIn(BaseModel):
    password: str | None = Field(default=None, max_length=128)
    code: str | None = Field(default=None, pattern=r"^\d{6}$", description="If MFA is on")


class DeletionOut(BaseModel):
    scheduled_for: datetime
    message: str


@router.post(
    "/me/deletion",
    response_model=DeletionOut,
    status_code=status.HTTP_202_ACCEPTED,
    description="Confirm with your password (and authenticator code if MFA is on). The account "
    "is deactivated now and anonymized after `ACCOUNT_DELETION_GRACE_DAYS`; logging in before "
    "then cancels it. Refused (409 `deletion_blocked`) while you lead open cases, have "
    "upcoming bookings or unfinished refunds. Cases, bookings and payments are kept without "
    "your personal details.",
)
async def request_deletion(
    data: DeletionIn, user: CurrentUser, token: TokenDep, session: SessionDep, audit: AuditDep
) -> DeletionOut:
    await hit(f"deletion:user:{user.id}", 5, 3600)
    scheduled = await account.request_deletion(
        session, user, token, data.password, data.code, audit
    )
    return DeletionOut(
        scheduled_for=scheduled,
        message="Your account is deactivated and will be deleted then. Log in to cancel.",
    )


@router.get(
    "/me/export",
    description="Download your profile, bookings, payments and notification settings as JSON. "
    "Limited to 5 per hour.",
    responses={200: {"content": {"application/json": {}}}},
)
async def export_my_data(user: CurrentUser, session: SessionDep, audit: AuditDep) -> JSONResponse:
    await hit(f"export:user:{user.id}", 5, 3600)
    data: dict[str, Any] = await account.export_data(session, user, audit)
    filename = f"legalpro-export-{datetime.now(UTC):%Y%m%d}.json"
    return JSONResponse(
        data,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.delete("/me/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def remove_avatar(user: CurrentUser, session: SessionDep) -> None:
    await avatars.remove_avatar(session, user)
