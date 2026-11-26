from fastapi import APIRouter, Depends, status

from app.core import rate_limit
from app.core.deps import SessionDep
from app.core.rate_limit import limit_by_ip
from app.modules.auth import service
from app.modules.auth.deps import CurrentUser
from app.modules.auth.schemas import (
    AdvocateRegisterIn,
    ClientRegisterIn,
    ForgotPasswordIn,
    GoogleLoginIn,
    LoginIn,
    LoginOut,
    MessageOut,
    MfaCodeIn,
    MfaSetupOut,
    MfaVerifyIn,
    RefreshIn,
    RegisterOut,
    ResetPasswordIn,
    TokenPair,
    VerifyEmailIn,
)
from app.modules.auth.tokens import issue_token_pair
from app.modules.users.service import to_user_out

router = APIRouter(prefix="/auth", tags=["auth"])

# Every auth endpoint is rate limited per client IP. Sensitive ones are also
# limited per account, so spreading attempts across many IPs doesn't help.
_register_limit = Depends(limit_by_ip("register", 10, 3600))
_login_limit = Depends(limit_by_ip("login", 10, 60))
_refresh_limit = Depends(limit_by_ip("refresh", 30, 60))
_mfa_limit = Depends(limit_by_ip("mfa", 10, 60))
_forgot_limit = Depends(limit_by_ip("forgot", 5, 3600))
_reset_limit = Depends(limit_by_ip("reset", 10, 3600))


@router.post(
    "/register/client",
    response_model=RegisterOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_register_limit],
)
async def register_client(data: ClientRegisterIn, session: SessionDep) -> RegisterOut:
    user = await service.register_client(session, data)
    return RegisterOut(user=await to_user_out(session, user), tokens=await issue_token_pair(user))


@router.post(
    "/register/advocate",
    response_model=RegisterOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_register_limit],
    description="Creates an unverified advocate. An admin must verify the profile before it "
    "appears in public search.",
)
async def register_advocate(data: AdvocateRegisterIn, session: SessionDep) -> RegisterOut:
    user = await service.register_advocate(session, data)
    return RegisterOut(user=await to_user_out(session, user), tokens=await issue_token_pair(user))


@router.post("/login", response_model=LoginOut, dependencies=[_login_limit])
async def login(data: LoginIn, session: SessionDep) -> LoginOut:
    await rate_limit.hit(f"login:email:{data.email}", 10, 900)
    user = await service.authenticate(session, data.email, data.password)
    return await service.complete_login(session, user)


@router.post("/login/google", response_model=LoginOut, dependencies=[_login_limit])
async def login_google(data: GoogleLoginIn, session: SessionDep) -> LoginOut:
    return await service.google_login(session, data.id_token)


@router.post("/token/refresh", response_model=TokenPair, dependencies=[_refresh_limit])
async def refresh(data: RefreshIn, session: SessionDep) -> TokenPair:
    return await service.refresh_tokens(session, data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_refresh_limit])
async def logout(data: RefreshIn) -> None:
    await service.logout(data.refresh_token)


@router.post(
    "/logout/all",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_refresh_limit],
    description="Revokes every refresh token and access token of the current user.",
)
async def logout_all(user: CurrentUser, session: SessionDep) -> None:
    await service.logout_everywhere(session, user)


# ---------------------------------------------------------------- email verification


@router.post(
    "/email/verify",
    response_model=MessageOut,
    dependencies=[Depends(limit_by_ip("verify_email", 20, 3600))],
    description="Confirms the address using the token from the verification email link.",
)
async def verify_email(data: VerifyEmailIn, session: SessionDep) -> MessageOut:
    await service.verify_email(session, data.token)
    return MessageOut(message="Email verified")


@router.post(
    "/email/verify/resend",
    response_model=MessageOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_ip("verify_email_resend", 10, 3600))],
)
async def resend_verification(user: CurrentUser, session: SessionDep) -> MessageOut:
    await rate_limit.hit(f"verify_email_resend:user:{user.id}", 3, 3600)
    await service.resend_verification_email(session, user)
    return MessageOut(message="Verification email sent")


# ---------------------------------------------------------------- MFA


@router.post("/mfa/setup", response_model=MfaSetupOut, dependencies=[_mfa_limit])
async def mfa_setup(user: CurrentUser) -> MfaSetupOut:
    """Step 1: get a secret + QR code. MFA is NOT enabled until `/mfa/confirm` succeeds."""
    return await service.mfa_setup(user)


@router.post("/mfa/confirm", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_mfa_limit])
async def mfa_confirm(data: MfaCodeIn, user: CurrentUser, session: SessionDep) -> None:
    """Step 2: prove the authenticator app works by sending its current code."""
    await rate_limit.hit(f"mfa_confirm:user:{user.id}", 5, 300)
    await service.mfa_confirm(session, user, data.code)


@router.post("/mfa/verify", response_model=LoginOut, dependencies=[_mfa_limit])
async def mfa_verify(data: MfaVerifyIn, session: SessionDep) -> LoginOut:
    """Exchange the `mfa_token` from `/auth/login` plus an authenticator code for tokens."""
    return await service.mfa_verify(session, data.mfa_token, data.code)


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_mfa_limit])
async def mfa_disable(data: MfaCodeIn, user: CurrentUser, session: SessionDep) -> None:
    await rate_limit.hit(f"mfa_disable:user:{user.id}", 5, 300)
    await service.mfa_disable(session, user, data.code)


# ---------------------------------------------------------------- password reset


@router.post(
    "/password/forgot",
    response_model=MessageOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_forgot_limit],
)
async def forgot_password(data: ForgotPasswordIn, session: SessionDep) -> MessageOut:
    await rate_limit.hit(f"forgot:email:{data.email}", 3, 3600)
    await service.forgot_password(session, data.email)
    return MessageOut(message="If an account exists for this email, a reset code has been sent.")


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_reset_limit])
async def reset_password(data: ResetPasswordIn, session: SessionDep) -> None:
    await rate_limit.hit(f"reset:email:{data.email}", 10, 3600)
    await service.reset_password(session, data.email, data.code, data.new_password)
