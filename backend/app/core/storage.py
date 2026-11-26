"""S3-compatible object storage. Objects are private; reads go through presigned URLs only."""

from typing import Any, Protocol
from urllib.parse import quote

import aioboto3
import botocore.session
from botocore.config import Config

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)


class StorageError(AppError):
    status_code = 502
    code = "storage_error"


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def presigned_get(
        self, key: str, *, filename: str, content_type: str, ttl_seconds: int
    ) -> str: ...

    async def delete(self, key: str) -> None: ...


def content_disposition(filename: str) -> str:
    """`attachment` (never inline) with an ASCII fallback plus the RFC 5987 UTF-8 name."""
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class S3Storage:
    def __init__(self) -> None:
        self._session = aioboto3.Session()
        self._config = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        # Sync botocore client, created once: presigning is local CPU work, no network.
        self._signer: Any = None

    def _client(self, endpoint_url: str | None):  # type: ignore[no-untyped-def]
        s = get_settings()
        return self._session.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=s.s3_access_key or None,
            aws_secret_access_key=s.s3_secret_key.get_secret_value() or None,
            region_name=s.s3_region,
            config=self._config,
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        s = get_settings()
        try:
            async with self._client(s.s3_endpoint_url) as client:
                await client.put_object(
                    Bucket=s.s3_bucket, Key=key, Body=data, ContentType=content_type
                )
        except Exception as exc:
            logger.exception("storage_put_failed", key=key)
            raise StorageError("File storage is unavailable; try again") from exc

    def _get_signer(self) -> Any:
        if self._signer is None:
            s = get_settings()
            self._signer = botocore.session.get_session().create_client(
                "s3",
                # Sign against the URL the browser will use (the host is part of the signature).
                endpoint_url=s.s3_public_endpoint_url or s.s3_endpoint_url,
                aws_access_key_id=s.s3_access_key or None,
                aws_secret_access_key=s.s3_secret_key.get_secret_value() or None,
                region_name=s.s3_region,
                config=self._config,
            )
        return self._signer

    async def presigned_get(
        self, key: str, *, filename: str, content_type: str, ttl_seconds: int
    ) -> str:
        url: str = self._get_signer().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": get_settings().s3_bucket,
                "Key": key,
                "ResponseContentDisposition": content_disposition(filename),
                "ResponseContentType": content_type,
            },
            ExpiresIn=ttl_seconds,
        )
        return url

    async def delete(self, key: str) -> None:
        s = get_settings()
        try:
            async with self._client(s.s3_endpoint_url) as client:
                await client.delete_object(Bucket=s.s3_bucket, Key=key)
        except Exception:
            logger.exception("storage_delete_failed", key=key)  # orphan; not fatal


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = S3Storage()
    return _storage


def set_storage(storage: Storage | None) -> None:
    """Swap the storage backend (tests use an in-memory fake)."""
    global _storage
    _storage = storage
