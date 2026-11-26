import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import decrypt_str, encrypt_str
from app.modules.users.models import User, UserRole
from app.scripts.rotate_field_encryption import rotate_all


def _use_keys(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    monkeypatch.setattr(get_settings(), "field_encryption_key", [SecretStr(k) for k in keys])


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "secret_key": "s" * 40,
        "database_url": "postgresql+asyncpg://u:p@db/x",
        "jwt_secret_key": "j" * 40,
        "field_encryption_key": Fernet.generate_key().decode(),
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


def test_field_encryption_key_must_be_a_fernet_key() -> None:
    with pytest.raises(ValidationError, match="Fernet keys"):
        _settings(field_encryption_key="not-a-key")
    with pytest.raises(ValidationError, match="at least one key"):
        _settings(field_encryption_key="")


def test_production_rejects_reused_or_duplicate_keys() -> None:
    key = Fernet.generate_key().decode()
    with pytest.raises(ValidationError, match="differ from SECRET_KEY"):
        _settings(secret_key=key, field_encryption_key=key)
    with pytest.raises(ValidationError, match="duplicate"):
        _settings(field_encryption_key=f"{key},{key}")


def test_multiple_keys_are_parsed_newest_first() -> None:
    new, old = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    s = _settings(field_encryption_key=f"{new}, {old}")
    assert [k.get_secret_value() for k in s.field_encryption_key] == [new, old]


def test_encryption_does_not_depend_on_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encrypt_str("totp-secret")
    monkeypatch.setattr(get_settings(), "secret_key", SecretStr("rotated-secret-key-" + "x" * 30))
    assert decrypt_str(token) == "totp-secret"


async def test_key_rotation_end_to_end(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    # 1. Secret stored under the old key.
    _use_keys(monkeypatch, old)
    user = User(
        email="rotate@example.com",
        role=UserRole.ADVOCATE,
        mfa_enabled=True,
        mfa_secret_encrypted=encrypt_str("JBSWY3DPEHPK3PXP"),
    )
    db_session.add(user)
    await db_session.commit()

    # 2. New key prepended: old values still decrypt, new values use the new key.
    _use_keys(monkeypatch, new, old)
    assert decrypt_str(user.mfa_secret_encrypted or "") == "JBSWY3DPEHPK3PXP"
    assert await rotate_all(db_session) == 1

    # 3. Old key removed: rotated values still decrypt.
    _use_keys(monkeypatch, new)
    stored = await db_session.scalar(
        select(User.mfa_secret_encrypted).where(User.email == "rotate@example.com")
    )
    assert stored is not None
    assert decrypt_str(stored) == "JBSWY3DPEHPK3PXP"

    # A value only the removed key can read is rejected loudly.
    _use_keys(monkeypatch, old)
    legacy = encrypt_str("x")
    _use_keys(monkeypatch, new)
    with pytest.raises(ValueError, match="FIELD_ENCRYPTION_KEY"):
        decrypt_str(legacy)
