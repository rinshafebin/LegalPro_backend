"""Import every module's models so `Base.metadata` is complete.

Alembic, SQLAdmin and the test suite import this module. Add each new
feature module's `models` import here.
"""

from app.core.db import Base
from app.modules.advocates.models import AdvocateProfile
from app.modules.auth.models import PasswordResetCode
from app.modules.clients.models import ClientProfile
from app.modules.users.models import User, UserRole

__all__ = ["AdvocateProfile", "Base", "ClientProfile", "PasswordResetCode", "User", "UserRole"]
