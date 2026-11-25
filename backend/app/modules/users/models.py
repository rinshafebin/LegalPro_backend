import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, Integer, String, Uuid, false, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin

if TYPE_CHECKING:
    from app.modules.advocates.models import AdvocateProfile
    from app.modules.clients.models import ClientProfile


class UserRole(StrEnum):
    CLIENT = "client"
    ADVOCATE = "advocate"
    ADMIN = "admin"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # NULL for accounts created through Google sign-in.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            length=20,
            create_constraint=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Fernet-encrypted TOTP secret; never returned by the API.
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(String(255))

    # Bumped on password reset / "log out everywhere"; tokens with an older version are rejected.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client_profile: Mapped["ClientProfile | None"] = relationship(
        back_populates="user", lazy="raise", cascade="all, delete-orphan", uselist=False
    )
    advocate_profile: Mapped["AdvocateProfile | None"] = relationship(
        back_populates="user",
        lazy="raise",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="AdvocateProfile.user_id",
    )

    def __repr__(self) -> str:
        return f"<User {self.id} {self.role}>"
