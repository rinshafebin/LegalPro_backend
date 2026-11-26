import re
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.modules.users.schemas import UserOut

_COMMON_PASSWORDS = frozenset(
    {"password", "password1", "password123", "12345678", "123456789", "qwerty123", "iloveyou",
     "admin123", "letmein1", "welcome1", "passw0rd", "abc12345", "11111111", "00000000"}
)  # fmt: skip


def _check_password(value: str) -> str:
    if value.lower() in _COMMON_PASSWORDS:
        raise ValueError("Password is too common")
    if value.isdigit():
        raise ValueError("Password cannot be entirely numeric")
    if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        raise ValueError("Password must contain at least one letter and one digit")
    return value


def _normalize_email(value: str) -> str:
    return value.strip().lower()


Email = Annotated[EmailStr, AfterValidator(_normalize_email)]
Password = Annotated[str, Field(min_length=8, max_length=128), AfterValidator(_check_password)]
Phone = Annotated[str, Field(pattern=r"^\+?[0-9 ()-]{7,20}$")]
OtpCode = Annotated[str, Field(pattern=r"^\d{6}$", examples=["123456"])]


class _PasswordNotEmail(BaseModel):
    email: Email
    password: Password

    @model_validator(mode="after")
    def _password_differs_from_email(self) -> Self:
        local_part = self.email.split("@", 1)[0]
        if len(local_part) >= 4 and local_part in self.password.lower():
            raise ValueError("Password is too similar to the email address")
        return self


class ClientRegisterIn(_PasswordNotEmail):
    full_name: str = Field(min_length=2, max_length=150)
    phone: Phone | None = None


class AdvocateRegisterIn(_PasswordNotEmail):
    full_name: str = Field(min_length=2, max_length=150)
    phone: Phone | None = None
    bar_council_id: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9/-]+$")


class LoginIn(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=128)


class GoogleLoginIn(BaseModel):
    id_token: str = Field(min_length=10, max_length=4096)


class RefreshIn(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int = Field(description="Access-token lifetime in seconds")


class LoginOut(BaseModel):
    """Either `tokens` is set, or `mfa_required` is true and `mfa_token` must be sent to
    `/auth/mfa/verify` along with the authenticator code."""

    mfa_required: bool = False
    mfa_token: str | None = None
    tokens: TokenPair | None = None
    user: UserOut | None = None


class RegisterOut(BaseModel):
    user: UserOut
    tokens: TokenPair


class MfaSetupOut(BaseModel):
    secret: str = Field(description="Base32 secret, for manual entry in the authenticator app")
    otpauth_uri: str
    qr_code_data_uri: str = Field(description="PNG QR code as a data: URI")


class MfaCodeIn(BaseModel):
    code: OtpCode


class MfaVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mfa_token: str
    code: OtpCode


class ForgotPasswordIn(BaseModel):
    email: Email


class ResetPasswordIn(BaseModel):
    email: Email
    code: OtpCode
    new_password: Password


class VerifyEmailIn(BaseModel):
    token: str = Field(min_length=10, max_length=1024)


class MessageOut(BaseModel):
    message: str
