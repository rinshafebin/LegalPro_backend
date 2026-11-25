import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin

if TYPE_CHECKING:
    from app.modules.users.models import User


class AdvocateProfile(TimestampMixin, Base):
    """Core advocate profile. Phase 3 adds specializations, languages, teams and search fields."""

    __tablename__ = "advocate_profiles"

    # The profile shares the user's id: User.id is the only advocate identifier.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    full_name: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(20))
    bar_council_id: Mapped[str] = mapped_column(String(50), unique=True)
    experience_years: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="advocate_profile", lazy="raise")
