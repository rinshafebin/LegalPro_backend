"""Profile images for clients and advocates.

Same checks as case documents (size limit while streaming, type from the content), limited
to PNG/JPEG. Every image is then decoded and re-encoded: that drops EXIF metadata (phone
photos carry GPS location, and advocate photos are public) and rejects anything that
isn't really an image or is absurdly large. Objects are private; reads use signed URLs.
"""

import io
import uuid

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.errors import ForbiddenError
from app.core.logging import get_logger
from app.core.storage import get_storage
from app.modules.advocates.models import AdvocateProfile
from app.modules.cases import files
from app.modules.clients.models import ClientProfile
from app.modules.users.models import User, UserRole

logger = get_logger(__name__)

IMAGE_TYPES = {"image/png": ("PNG", ".png"), "image/jpeg": ("JPEG", ".jpg")}


def _reencode(data: bytes, content_type: str) -> bytes:
    max_side = get_settings().avatar_max_pixels
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size  # header only; nothing decoded yet
            if width > max_side or height > max_side:
                raise files.UnsupportedFileTypeError(
                    f"Image must be at most {max_side}x{max_side} pixels"
                )
            probe.load()
            fmt = IMAGE_TYPES[content_type][0]
            image = probe.convert("RGB") if fmt == "JPEG" else probe.copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise files.UnsupportedFileTypeError("The file is not a valid PNG or JPEG image") from exc
    out = io.BytesIO()
    image.save(out, format=fmt, optimize=True)  # no exif/info passed: metadata is dropped
    return out.getvalue()


async def _profile(session: AsyncSession, user: User) -> ClientProfile | AdvocateProfile:
    profile: ClientProfile | AdvocateProfile | None = None
    if user.role is UserRole.CLIENT:
        profile = await session.get(ClientProfile, user.id)
    elif user.role is UserRole.ADVOCATE:
        profile = await session.get(AdvocateProfile, user.id)
    if profile is None:
        raise ForbiddenError("Only clients and advocates have profile images")
    return profile


async def avatar_url(key: str | None) -> str | None:
    if key is None:
        return None
    ext = ".jpg" if key.endswith(".jpg") else ".png"
    content_type = "image/jpeg" if ext == ".jpg" else "image/png"
    return await get_storage().presigned_get(
        key,
        filename=f"avatar{ext}",
        content_type=content_type,
        ttl_seconds=get_settings().s3_presign_ttl_seconds,
    )


async def set_avatar(session: AsyncSession, user: User, upload: UploadFile) -> str | None:
    profile = await _profile(session, user)
    data = await files.read_limited(upload, get_settings().avatar_max_mb * 1024 * 1024)
    content_type = files.detect_type(data, upload.filename or "")
    if content_type not in IMAGE_TYPES:
        raise files.UnsupportedFileTypeError("Profile images must be PNG or JPEG")
    clean = await run_in_threadpool(_reencode, data, content_type)

    key = f"avatars/{user.id}/{uuid.uuid4()}{IMAGE_TYPES[content_type][1]}"
    await get_storage().put(key, clean, content_type)
    old_key = profile.avatar_key
    profile.avatar_key = key
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await get_storage().delete(key)
        raise
    if old_key:
        await get_storage().delete(old_key)
    logger.info("avatar_updated", user_id=str(user.id))
    return await avatar_url(key)


async def remove_avatar(session: AsyncSession, user: User) -> None:
    profile = await _profile(session, user)
    old_key, profile.avatar_key = profile.avatar_key, None
    await session.commit()
    if old_key:
        await get_storage().delete(old_key)
