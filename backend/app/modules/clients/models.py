import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin

if TYPE_CHECKING:
    from app.modules.users.models import User


class ClientProfile(TimestampMixin, Base):
    __tablename__ = "client_profiles"

    # The profile shares the user's id: User.id is the only client identifier.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    full_name: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(20))
    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    pincode: Mapped[str | None] = mapped_column(String(12))
    # S3 object key; served through signed URLs only.
    avatar_key: Mapped[str | None] = mapped_column(String(512))

    user: Mapped["User"] = relationship(back_populates="client_profile", lazy="raise")
