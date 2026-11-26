import base64
import io
from datetime import UTC, datetime, timedelta

import pyotp
import qrcode
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.email import enqueue_email
from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    UnauthorizedError,
)
from app.core.logging import get_logger
from app.core.security import (
    InvalidTokenError,
    TokenType,
    codes_match,
    create_email_verification_token,
    decode_token,
    decrypt_str,
    encrypt_str,
    generate_numeric_code,
    hash_code,
    hash_password,
    read_email_verification_token,
    verify_password,
)
from app.modules.advocates.models import AdvocateProfile
from app.modules.auth import emails, google_oauth, tokens
from app.modules.auth.models import PasswordResetCode
from app.modules.auth.schemas import (
    AdvocateRegisterIn,
    ClientRegisterIn,
    LoginOut,
    MfaSetupOut,
    TokenPair,
)
from app.modules.clients.models import ClientProfile
from app.modules.users.models import User, UserRole
from app.modules.users.service import get_profile, to_user_out

logger = get_logger(__name__)

RESET_CODE_MAX_ATTEMPTS = 5


async def _queue_email(to: str, message: tuple[str, str]) -> None:
    subject, body = message
    await enqueue_email(to, subject, body)


async def _get_user_by_email(session: AsyncSession, email: str) -> User | None:
    user: User | None = await session.scalar(select(User).where(User.email == email))
    return user


async def _commit_new_user(session: AsyncSession, user: User) -> None:
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("An account with these details already exists") from exc


# ---------------------------------------------------------------- registration


async def register_client(session: AsyncSession, data: ClientRegisterIn) -> User:
    if await _get_user_by_email(session, data.email):
        raise ConflictError("An account with this email already exists")

    user = User(
        email=data.email,
        password_hash=await hash_password(data.password),
        role=UserRole.CLIENT,
    )
    user.client_profile = ClientProfile(full_name=data.full_name, phone=data.phone)
    await _commit_new_user(session, user)
    await send_verification_email(user, data.full_name)
    logger.info("user_registered", user_id=str(user.id), role=user.role.value)
    return user


async def register_advocate(session: AsyncSession, data: AdvocateRegisterIn) -> User:
    if await _get_user_by_email(session, data.email):
        raise ConflictError("An account with this email already exists")
    bar_id = data.bar_council_id.upper()
    if await session.scalar(
        select(AdvocateProfile.user_id).where(AdvocateProfile.bar_council_id == bar_id)
    ):
        raise ConflictError("This bar council ID is already registered")

    user = User(
        email=data.email,
        password_hash=await hash_password(data.password),
        role=UserRole.ADVOCATE,
    )
    user.advocate_profile = AdvocateProfile(
        full_name=data.full_name, phone=data.phone, bar_council_id=bar_id
    )
    await _commit_new_user(session, user)
    await send_verification_email(user, data.full_name)
    logger.info("user_registered", user_id=str(user.id), role=user.role.value)
    return user


# ---------------------------------------------------------------- email verification


def verification_link(user: User) -> str:
    token = create_email_verification_token(user.id, user.email)
    return f"{get_settings().frontend_url.rstrip('/')}/verify-email?token={token}"


async def send_verification_email(user: User, full_name: str) -> None:
    await _queue_email(user.email, emails.verify_email(full_name, verification_link(user)))


async def verify_email(session: AsyncSession, token: str) -> User:
    try:
        user_id, email = read_email_verification_token(token)
    except InvalidTokenError as exc:
        raise BadRequestError(str(exc), code="invalid_token") from exc

    user = await session.get(User, user_id)
    # The token is bound to the address it was sent to.
    if user is None or not user.is_active or user.email != email:
        raise BadRequestError("Invalid verification link", code="invalid_token")
    if not user.email_verified:
        user.email_verified = True
        await session.commit()
        logger.info("email_verified", user_id=str(user.id))
    return user


async def resend_verification_email(session: AsyncSession, user: User) -> None:
    if user.email_verified:
        raise ConflictError("Email is already verified")
    profile = await get_profile(session, user)
    full_name = profile.full_name if profile is not None else user.email
    await send_verification_email(user, full_name)


# ---------------------------------------------------------------- login


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    user = await _get_user_by_email(session, email)
    password_ok = await verify_password(password, user.password_hash if user else None)
    if user is None or not password_ok:
        raise UnauthorizedError("Invalid email or password")
    if not user.is_active:
        raise ForbiddenError("This account is disabled")
    return user


async def complete_login(session: AsyncSession, user: User) -> LoginOut:
    """Return tokens, or an MFA challenge if the user has MFA enabled."""
    if user.mfa_enabled:
        return LoginOut(mfa_required=True, mfa_token=tokens.issue_mfa_token(user))

    user.last_login_at = datetime.now(UTC)
    await session.commit()
    pair = await tokens.issue_token_pair(user)
    return LoginOut(tokens=pair, user=await to_user_out(session, user))


async def google_login(session: AsyncSession, id_token: str) -> LoginOut:
    client_id = get_settings().google_client_id
    if not client_id:
        raise AppError("Google sign-in is not configured", code="not_configured", status_code=503)
    try:
        claims = await google_oauth.verify_id_token(id_token, client_id)
    except ValueError as exc:
        raise UnauthorizedError("Invalid Google token") from exc

    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise BadRequestError("Google account has no verified email", code="email_not_verified")

    user = await _get_user_by_email(session, email)
    if user is None:
        full_name = str(claims.get("name") or email.split("@", 1)[0])[:150]
        user = User(email=email, password_hash=None, role=UserRole.CLIENT, email_verified=True)
        user.client_profile = ClientProfile(full_name=full_name)
        await _commit_new_user(session, user)
        await _queue_email(user.email, emails.welcome(full_name))
        logger.info("user_registered", user_id=str(user.id), role="client", via="google")
    elif not user.is_active:
        raise ForbiddenError("This account is disabled")
    elif not user.email_verified:
        user.email_verified = True  # Google has proven ownership of the address

    return await complete_login(session, user)


# ---------------------------------------------------------------- refresh / logout


async def refresh_tokens(session: AsyncSession, refresh_token: str) -> TokenPair:
    try:
        payload = decode_token(refresh_token, TokenType.REFRESH)
    except InvalidTokenError as exc:
        raise UnauthorizedError(str(exc)) from exc

    if not await tokens.consume_refresh(payload):
        # A refresh token was presented twice: assume it leaked and end every session.
        await tokens.revoke_all_sessions(payload.sub)
        logger.warning("refresh_token_reuse_detected", user_id=str(payload.sub))
        raise UnauthorizedError("Refresh token is no longer valid")

    user = await session.get(User, payload.sub)
    if user is None or not user.is_active or payload.ver != user.token_version:
        raise UnauthorizedError("Refresh token is no longer valid")
    return await tokens.issue_token_pair(user)


async def logout(refresh_token: str) -> None:
    try:
        payload = decode_token(refresh_token, TokenType.REFRESH)
    except InvalidTokenError:
        return  # already unusable; logout is idempotent
    await tokens.consume_refresh(payload)


async def logout_everywhere(session: AsyncSession, user: User) -> None:
    user.token_version += 1  # invalidates every outstanding access token
    await session.commit()
    await tokens.revoke_all_sessions(user.id)


# ---------------------------------------------------------------- MFA


def _qr_data_uri(data: str) -> str:
    buffer = io.BytesIO()
    qrcode.make(data).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


async def mfa_setup(user: User) -> MfaSetupOut:
    if user.mfa_enabled:
        raise ConflictError("MFA is already enabled")
    secret = pyotp.random_base32()
    await tokens.store_pending_mfa_secret(user.id, encrypt_str(secret))
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email, issuer_name=get_settings().mfa_issuer
    )
    return MfaSetupOut(secret=secret, otpauth_uri=uri, qr_code_data_uri=_qr_data_uri(uri))


async def mfa_confirm(session: AsyncSession, user: User, code: str) -> None:
    if user.mfa_enabled:
        raise ConflictError("MFA is already enabled")
    encrypted = await tokens.get_pending_mfa_secret(user.id)
    if encrypted is None:
        raise BadRequestError("No MFA setup in progress; call /auth/mfa/setup first")
    if not await tokens.verify_totp(user.id, decrypt_str(encrypted), code):
        raise BadRequestError("Invalid authentication code", code="invalid_code")

    await tokens.pop_pending_mfa_secret(user.id)
    user.mfa_secret_encrypted = encrypted
    user.mfa_enabled = True
    await session.commit()
    logger.info("mfa_enabled", user_id=str(user.id))


async def mfa_verify(session: AsyncSession, mfa_token: str, code: str) -> LoginOut:
    try:
        payload = decode_token(mfa_token, TokenType.MFA)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or expired MFA token; log in again") from exc
    if not await tokens.register_mfa_attempt(payload):
        raise UnauthorizedError("MFA token is no longer valid; log in again")

    user = await session.get(User, payload.sub)
    if (
        user is None
        or not user.is_active
        or payload.ver != user.token_version
        or not user.mfa_enabled
        or user.mfa_secret_encrypted is None
    ):
        raise UnauthorizedError("MFA token is no longer valid; log in again")

    if not await tokens.verify_totp(user.id, decrypt_str(user.mfa_secret_encrypted), code):
        raise UnauthorizedError("Invalid authentication code", code="invalid_code")
    if not await tokens.mark_mfa_token_used(payload):
        raise UnauthorizedError("MFA token has already been used; log in again")

    user.last_login_at = datetime.now(UTC)
    await session.commit()
    pair = await tokens.issue_token_pair(user)
    return LoginOut(tokens=pair, user=await to_user_out(session, user))


async def mfa_disable(session: AsyncSession, user: User, code: str) -> None:
    if not user.mfa_enabled or user.mfa_secret_encrypted is None:
        raise BadRequestError("MFA is not enabled")
    if not await tokens.verify_totp(user.id, decrypt_str(user.mfa_secret_encrypted), code):
        raise BadRequestError("Invalid authentication code", code="invalid_code")
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    await session.commit()
    logger.info("mfa_disabled", user_id=str(user.id))


# ---------------------------------------------------------------- password reset


async def forgot_password(session: AsyncSession, email: str) -> None:
    """Always succeeds from the caller's point of view, so it can't reveal which emails exist."""
    user = await _get_user_by_email(session, email)
    if user is None or not user.is_active:
        logger.info("password_reset_requested_unknown")
        return

    now = datetime.now(UTC)
    # A new code supersedes any earlier unused code.
    await session.execute(
        update(PasswordResetCode)
        .where(PasswordResetCode.user_id == user.id, PasswordResetCode.used_at.is_(None))
        .values(used_at=now)
    )
    code = generate_numeric_code()
    session.add(
        PasswordResetCode(
            user_id=user.id,
            code_hash=hash_code(code),
            expires_at=now + timedelta(seconds=get_settings().otp_ttl_seconds),
        )
    )
    await session.commit()
    await _queue_email(user.email, emails.password_reset(code))
    logger.info("password_reset_requested", user_id=str(user.id))


async def reset_password(session: AsyncSession, email: str, code: str, new_password: str) -> None:
    invalid = BadRequestError("Invalid or expired code", code="invalid_code")

    user = await _get_user_by_email(session, email)
    if user is None or not user.is_active:
        raise invalid

    record = await session.scalar(
        select(PasswordResetCode)
        .where(PasswordResetCode.user_id == user.id, PasswordResetCode.used_at.is_(None))
        .order_by(PasswordResetCode.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if record is None or record.expires_at <= now or record.attempts >= RESET_CODE_MAX_ATTEMPTS:
        raise invalid
    if not codes_match(code, record.code_hash):
        record.attempts += 1
        await session.commit()
        raise invalid

    record.used_at = now
    user.password_hash = await hash_password(new_password)
    user.token_version += 1  # log out every existing session
    await session.commit()
    await tokens.revoke_all_sessions(user.id)
    logger.info("password_reset_completed", user_id=str(user.id))
