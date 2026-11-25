"""Re-encrypt stored secrets under the primary FIELD_ENCRYPTION_KEY.

Rotation procedure:
  1. Prepend a new key:  FIELD_ENCRYPTION_KEY=<new>,<old>   and deploy.
  2. Run:                uv run python -m app.scripts.rotate_field_encryption
  3. Wait until queued jobs and pending MFA setups (10 min) made with the old key are gone,
     then remove the old key and deploy again.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every mapper before the first query
from app.core.db import SessionLocal, engine
from app.core.logging import configure_logging, get_logger
from app.core.security import rotate_encrypted_str
from app.modules.users.models import User

logger = get_logger(__name__)
BATCH_SIZE = 500


async def rotate_all(session: AsyncSession) -> int:
    """Rotate every encrypted column. Returns the number of rows updated."""
    updated = 0
    last_id = None
    while True:
        stmt = (
            select(User)
            .where(User.mfa_secret_encrypted.is_not(None))
            .order_by(User.id)
            .limit(BATCH_SIZE)
            .with_for_update()
        )
        if last_id is not None:
            stmt = stmt.where(User.id > last_id)
        users = (await session.scalars(stmt)).all()
        if not users:
            break
        for user in users:
            assert user.mfa_secret_encrypted is not None  # noqa: S101 - filtered above
            user.mfa_secret_encrypted = rotate_encrypted_str(user.mfa_secret_encrypted)
        await session.commit()
        updated += len(users)
        last_id = users[-1].id
    return updated


async def main() -> None:
    configure_logging()
    async with SessionLocal() as session:
        count = await rotate_all(session)
    await engine.dispose()
    logger.info("field_encryption_rotated", rows=count)


if __name__ == "__main__":
    asyncio.run(main())
