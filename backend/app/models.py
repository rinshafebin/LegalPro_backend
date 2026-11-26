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
from app.modules.audit.models import AuditLog
from app.modules.auth.models import PasswordResetCode
from app.modules.bookings.models import AvailabilityRule, Booking
from app.modules.cases.models import Case, CaseDocument, CaseMember, CaseNote, Hearing
from app.modules.chat.models import ChatMessage, ChatReadState, ChatRoom
from app.modules.clients.models import ClientProfile
from app.modules.notifications.models import Notification, NotificationPreference
from app.modules.payments.models import Payment, WebhookEvent
from app.modules.users.models import User, UserRole

__all__ = [
    "AdvocateProfile",
    "AdvocateTeam",
    "AuditLog",
    "AvailabilityRule",
    "Base",
    "Booking",
    "Case",
    "CaseDocument",
    "CaseMember",
    "CaseNote",
    "ChatMessage",
    "ChatReadState",
    "ChatRoom",
    "ClientProfile",
    "Hearing",
    "Notification",
    "NotificationPreference",
    "PasswordResetCode",
    "Payment",
    "Specialization",
    "TeamMember",
    "User",
    "UserRole",
    "WebhookEvent",
    "advocate_specializations",
]
