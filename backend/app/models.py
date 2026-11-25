"""Import every module's models so `Base.metadata` is complete.

Alembic, SQLAdmin and the test suite import this module. Add each new
feature module's `models` import here.
"""

from app.core.db import Base

__all__ = ["Base"]
