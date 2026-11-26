"""Create an admin user (admins cannot self-register).

    uv run python -m app.scripts.create_admin admin@example.com

The password is read interactively (or from ADMIN_PASSWORD for automation).
Admins must use two-factor authentication, so MFA is switched on here: the script prints
the authenticator secret and setup URI once. Add it to an authenticator app straight away.
"""

import argparse
import asyncio
import getpass
import os
import sys

import pyotp
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper before the first query
from app.core.config import get_settings
from app.core.db import SessionLocal, engine
from app.core.security import encrypt_str, hash_password
from app.modules.auth.schemas import Email, Password
from app.modules.users.models import User, UserRole


async def create_admin(email: str, password: str, totp_secret: str) -> User:
    async with SessionLocal() as session:
        if await session.scalar(select(User).where(User.email == email)):
            raise SystemExit(f"A user with email {email} already exists")
        user = User(
            email=email,
            password_hash=await hash_password(password),
            role=UserRole.ADMIN,
            email_verified=True,
            mfa_enabled=True,
            mfa_secret_encrypted=encrypt_str(totp_secret),
        )
        session.add(user)
        await session.commit()
        return user


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    args = parser.parse_args()

    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    try:
        email = TypeAdapter(Email).validate_python(args.email)
        TypeAdapter(Password).validate_python(password)
    except ValidationError as exc:
        sys.exit(f"Invalid input: {exc.errors()[0]['msg']}")

    secret = pyotp.random_base32()

    async def _run() -> User:
        try:
            return await create_admin(email, password, secret)
        finally:
            await engine.dispose()

    user = asyncio.run(_run())
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email, issuer_name=get_settings().mfa_issuer
    )
    sys.stdout.write(
        f"Admin created: {user.email} ({user.id})\n"
        "Two-factor authentication is required for admins. Add this to an authenticator app\n"
        "now; it is shown only once:\n"
        f"  secret: {secret}\n"
        f"  uri:    {uri}\n"
    )


if __name__ == "__main__":
    main()
