import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin

if TYPE_CHECKING:
    from app.modules.users.models import User


advocate_specializations = Table(
    "advocate_specializations",
    Base.metadata,
    Column(
        "advocate_id",
        ForeignKey("advocate_profiles.user_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "specialization_id",
        ForeignKey("specializations.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    ),
)


class Specialization(Base):
    """Practice area (lookup table, managed by admins)."""

    __tablename__ = "specializations"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class AdvocateProfile(TimestampMixin, Base):
    __tablename__ = "advocate_profiles"
    __table_args__ = (
        CheckConstraint("experience_years BETWEEN 0 AND 80", name="experience_years_range"),
        CheckConstraint("consultation_fee >= 0", name="consultation_fee_non_negative"),
        # Trigram index makes `full_name ILIKE '%term%'` searches fast (needs pg_trgm).
        Index(
            "ix_advocate_profiles_full_name_trgm",
            "full_name",
            postgresql_using="gin",
            postgresql_ops={"full_name": "gin_trgm_ops"},
        ),
        Index("ix_advocate_profiles_city_lower", func.lower(text("city"))),
        Index("ix_advocate_profiles_languages", "languages", postgresql_using="gin"),
        Index(
            "ix_advocate_profiles_verified_experience",
            "experience_years",
            postgresql_where=text("is_verified"),
        ),
    )

    # The profile shares the user's id: User.id is the only advocate identifier.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    full_name: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(20))
    gender: Mapped[str | None] = mapped_column(String(20))
    bio: Mapped[str | None] = mapped_column(Text)
    bar_council_id: Mapped[str] = mapped_column(String(50), unique=True)
    enrollment_year: Mapped[int | None] = mapped_column(Integer)
    experience_years: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Lower-cased language names, e.g. ["english", "malayalam"].
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), default=list, server_default="{}"
    )
    consultation_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # IANA zone the advocate's weekly availability is written in. Bookings are stored in UTC.
    timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Kolkata", server_default="Asia/Kolkata"
    )

    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    pincode: Mapped[str | None] = mapped_column(String(12))

    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    user: Mapped["User"] = relationship(
        back_populates="advocate_profile", lazy="raise", foreign_keys=[user_id]
    )
    specializations: Mapped[list[Specialization]] = relationship(
        secondary=advocate_specializations, lazy="raise", order_by=Specialization.name
    )


class AdvocateTeam(TimestampMixin, Base):
    __tablename__ = "advocate_teams"
    __table_args__ = (UniqueConstraint("lead_id", "name", name="uq_advocate_teams_lead_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    members: Mapped[list["TeamMember"]] = relationship(
        back_populates="team",
        lazy="raise",
        cascade="all, delete-orphan",
        order_by="TeamMember.joined_at",
    )


class TeamMember(Base):
    __tablename__ = "advocate_team_members"

    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("advocate_teams.id", ondelete="CASCADE"), primary_key=True
    )
    advocate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    team: Mapped[AdvocateTeam] = relationship(back_populates="members", lazy="raise")

    __mapper_args__ = {"eager_defaults": True}
