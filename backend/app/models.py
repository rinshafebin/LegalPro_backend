"""Import every module's models so `Base.metadata` is complete.

Alembic, SQLAdmin and the test suite import this module. Add each new
feature module's `models` import here.
"""

from app.core.db import Base
from app.modules.advocates.models import (
    AdvocateProfile,
    AdvocateTeam,
    Specialization,
    TeamMember,
    advocate_specializations,
)
from app.modules.auth.models import PasswordResetCode
from app.modules.bookings.models import AvailabilityRule, Booking
from app.modules.cases.models import Case, CaseDocument, CaseMember, CaseNote, Hearing
from app.modules.clients.models import ClientProfile
from app.modules.payments.models import Payment, WebhookEvent
from app.modules.users.models import User, UserRole

__all__ = [
    "AdvocateProfile",
    "AdvocateTeam",
    "AvailabilityRule",
    "Base",
    "Booking",
    "Case",
    "CaseDocument",
    "CaseMember",
    "CaseNote",
    "ClientProfile",
    "Hearing",
    "PasswordResetCode",
    "Payment",
    "Specialization",
    "TeamMember",
    "User",
    "UserRole",
    "WebhookEvent",
    "advocate_specializations",
]
