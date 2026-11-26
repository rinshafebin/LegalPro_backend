"""Outgoing email over SMTP, always sent from a background job.

Job arguments are stored in Redis and printed in worker logs, and emails carry
secrets such as reset codes. So the whole message (recipient, subject, body) is
encrypted before it's queued and decrypted only inside the job.
"""

import json
from email.message import EmailMessage

import aiosmtplib

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import decrypt_str, encrypt_str

logger = get_logger(__name__)


def seal_message(to: str, subject: str, body: str) -> str:
    return encrypt_str(json.dumps({"to": to, "subject": subject, "body": body}))


def open_message(sealed: str) -> dict[str, str]:
    data: dict[str, str] = json.loads(decrypt_str(sealed))
    return data


async def enqueue_email(to: str, subject: str, body: str) -> None:
    from app.workers.queue import get_job_queue  # local import: workers depend on core

    await get_job_queue().enqueue("send_email", sealed=seal_message(to, subject, body))


async def send_email(to: str, subject: str, body: str) -> None:
    s = get_settings()
    message = EmailMessage()
    message["From"] = s.email_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    await aiosmtplib.send(
        message,
        hostname=s.smtp_host,
        port=s.smtp_port,
        username=s.smtp_user or None,
        password=s.smtp_password.get_secret_value() or None,
        start_tls=s.smtp_use_tls,
        timeout=15,
    )
    logger.info("email_sent", subject=subject)
