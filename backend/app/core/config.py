"""Application settings, loaded from environment variables and `.env`."""

from functools import lru_cache
from typing import Annotated, Literal, Self

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: `METRICS_TOKEN=` (empty) means "not set", never "set to empty".
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

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
    # Host headers the API answers to (TrustedHostMiddleware). Health probes are exempt.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])
    # Swagger UI / ReDoc / openapi.json. Default: on everywhere except production.
    api_docs_enabled: bool | None = None
    # Request bodies: JSON and other non-multipart bodies vs multipart uploads.
    max_json_body_kb: int = 1024

    # Fernet keys for at-rest field encryption (MFA secrets, queued email payloads).
    # Comma-separated, newest first: the first key encrypts, all keys decrypt. To rotate,
    # prepend a new key, run `python -m app.scripts.rotate_field_encryption`, then drop the old.
    field_encryption_key: Annotated[list[SecretStr], NoDecode]

    # --- Outbound calls (Razorpay, Google, S3); SMTP uses its own timeout ---
    external_connect_timeout_seconds: float = 5
    external_read_timeout_seconds: float = 15

    # --- Infrastructure ---
    database_url: str
    test_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    test_redis_url: str | None = None
    db_echo: bool = False
    # Per API/worker process. Keep pool_size + max_overflow, times processes, under
    # Postgres max_connections.
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout_seconds: int = 10
    db_pool_recycle_seconds: int = 1800
    # Kill runaway queries and transactions left open by a crashed request.
    db_statement_timeout_ms: int = 15_000
    db_idle_in_transaction_timeout_ms: int = 60_000

    # --- Observability ---
    sentry_dsn: SecretStr | None = None  # error monitoring is off unless set
    sentry_traces_sample_rate: float = 0.0
    # /metrics (Prometheus) is disabled unless set; scrapers send `Authorization: Bearer ...`.
    metrics_token: SecretStr | None = None

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
    # Account deletion: deactivated at once, personal data wiped after this many days
    # unless the user logs back in.
    account_deletion_grace_days: int = 14

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
    avatar_max_mb: int = 2
    avatar_max_pixels: int = 4096  # per side

    # --- Realtime / chat ---
    ws_ticket_ttl_seconds: int = 30
    # Browsers always send Origin; it must be in CORS_ORIGINS. Non-browser clients that send
    # no Origin are refused unless this is true.
    ws_allow_missing_origin: bool = False
    chat_rate_limit_messages: int = 20
    chat_rate_limit_window_seconds: int = 10
    chat_attachment_max_mb: int = 20
    chat_edit_window_minutes: int = 15
    # At most one "new message" notification (and email) per room per recipient per window.
    chat_notify_throttle_minutes: int = 10

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
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
            if self.ws_allow_missing_origin:
                raise ValueError("WS_ALLOW_MISSING_ORIGIN must be false in production")
            if not self.allowed_hosts or "*" in self.allowed_hosts:
                raise ValueError("ALLOWED_HOSTS must list the API's hostnames in production")
            if not self.frontend_url.startswith("https://"):
                raise ValueError("FRONTEND_URL must use https in production")
            if any(not o.startswith("https://") for o in self.cors_origins):
                raise ValueError("CORS_ORIGINS must all use https in production")
            if not (
                self.razorpay_key_id
                and self.razorpay_key_secret.get_secret_value()
                and self.razorpay_webhook_secret.get_secret_value()
            ):
                raise ValueError(
                    "RAZORPAY_KEY_ID/KEY_SECRET/WEBHOOK_SECRET are required in production"
                )
            if not (self.s3_access_key and self.s3_secret_key.get_secret_value()):
                raise ValueError("S3_ACCESS_KEY/S3_SECRET_KEY are required in production")
            if self.metrics_token is not None and len(self.metrics_token.get_secret_value()) < 32:
                raise ValueError("METRICS_TOKEN must be at least 32 characters")
        return self

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return not self.is_production

    @property
    def max_upload_body_bytes(self) -> int:
        """Largest multipart body: the biggest file limit plus 1 MB for form overhead."""
        largest = max(self.case_document_max_mb, self.chat_attachment_max_mb, self.avatar_max_mb)
        return (largest + 1) * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
