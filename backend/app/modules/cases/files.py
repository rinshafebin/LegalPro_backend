"""Upload validation: size limit while streaming, and file type by content (magic bytes).

The client's declared Content-Type and file extension are never trusted.
"""

import io
import re
import unicodedata
import zipfile
from pathlib import PurePath

from fastapi import UploadFile

from app.core.errors import AppError

ALLOWED_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}
_CHUNK = 1024 * 1024


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedFileTypeError(AppError):
    status_code = 415
    code = "unsupported_media_type"


async def read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Read the upload, aborting as soon as it exceeds `max_bytes`."""
    buffer = bytearray()
    while chunk := await upload.read(_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise PayloadTooLargeError(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit")
    if not buffer:
        raise AppError("File is empty", code="empty_file", status_code=400)
    return bytes(buffer)


def _is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:  # reads the index only
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" in names and "word/document.xml" in names


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def detect_type(data: bytes, filename: str) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"PK\x03\x04") and _is_docx(data):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if filename.lower().endswith(".txt") and _is_text(data):
        return "text/plain"
    raise UnsupportedFileTypeError("Allowed file types: PDF, PNG, JPEG, DOCX, TXT")


def safe_filename(raw: str | None, content_type: str) -> str:
    """Display name only (never used in storage keys); the extension matches the real type."""
    name = PurePath((raw or "").replace("\\", "/")).name
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[^\w .()-]", "_", name).strip(" .") or "document"
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem[:150]}{ALLOWED_TYPES[content_type]}"
