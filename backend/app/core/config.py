"""Application settings, loaded from environment variables and `.env`."""

from functools import lru_cache
from typing import Annotated, Literal, Self

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "Advocate & Client Management API"
    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    api_prefix: str = "/api/v1"
    secret_key: SecretStr
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Base URL of the web app; used for links in emails (e.g. email verification).
    frontend_url: str = "http://localhost:5173"

    # Fernet keys for at-rest field encryption (MFA secrets, queued email payloads).
    # Comma-separated, newest first: the first key encrypts, all keys decrypt. To rotate,
    # prepend a new key, run `python -m app.scripts.rotate_field_encryption`, then drop the old.
    field_encryption_key: Annotated[list[SecretStr], NoDecode]

    # --- Infrastructure ---
    database_url: str
    test_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    test_redis_url: str | None = None
    db_echo: bool = False

    # --- Auth ---
    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    mfa_token_ttl_seconds: int = 300
    otp_ttl_seconds: int = 300
    google_client_id: str = ""
    mfa_issuer: str = "Advocate Platform"
    email_verification_ttl_hours: int = 48

    # --- Email ---
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_use_tls: bool = False
    email_from: str = "no-reply@example.com"

    # --- Bookings ---
    default_advocate_timezone: str = "Asia/Kolkata"
    booking_horizon_days: int = 60
    booking_min_lead_minutes: int = 30
    booking_payment_timeout_minutes: int = 15
    booking_currency: str = "INR"
    # Client cancellations at least this long before the start get a full refund; later, none.
    cancellation_full_refund_hours: int = 24
    # Confirmed bookings can be rescheduled until this long before the start.
    reschedule_cutoff_hours: int = 24

    # --- Payments (Razorpay) ---
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")

    # --- Object storage (S3-compatible) ---
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: SecretStr = SecretStr("")
    s3_bucket: str = "advocate-files"
    s3_presign_ttl_seconds: int = 300
    case_document_max_mb: int = 20

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("field_encryption_key", mode="before")
    @classmethod
    def _split_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return [key.strip() for key in value.split(",") if key.strip()]
        return value

    @field_validator("field_encryption_key")
    @classmethod
    def _check_fernet_keys(cls, keys: list[SecretStr]) -> list[SecretStr]:
        if not keys:
            raise ValueError("FIELD_ENCRYPTION_KEY needs at least one key")
        for key in keys:
            try:
                Fernet(key.get_secret_value())
            except ValueError as exc:
                raise ValueError(
                    "FIELD_ENCRYPTION_KEY entries must be Fernet keys "
                    '(generate: python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())")'
                ) from exc
        return keys

    @model_validator(mode="after")
    def _apply_test_overrides(self) -> Self:
        if self.environment == "test":
            if self.test_database_url:
                self.database_url = self.test_database_url
            if self.test_redis_url:
                self.redis_url = self.test_redis_url
        return self

    @model_validator(mode="after")
    def _check_production(self) -> Self:
        if self.environment == "production":
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if "*" in self.cors_origins:
                raise ValueError("Wildcard CORS origin is not allowed in production")
            if len(self.jwt_secret_key.get_secret_value()) < 32:
                raise ValueError("JWT_SECRET_KEY must be at least 32 characters in production")
            if len(self.secret_key.get_secret_value()) < 32:
                raise ValueError("SECRET_KEY must be at least 32 characters in production")
            keys = {k.get_secret_value() for k in self.field_encryption_key}
            others = {self.secret_key.get_secret_value(), self.jwt_secret_key.get_secret_value()}
            if keys & others:
                raise ValueError("FIELD_ENCRYPTION_KEY must differ from SECRET_KEY/JWT_SECRET_KEY")
            if len(keys) != len(self.field_encryption_key):
                raise ValueError("FIELD_ENCRYPTION_KEY contains duplicate keys")
        return self

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
