"""Background job functions. Each one must be listed in `WorkerSettings.functions`."""

from typing import Any

from app.core import email


async def heartbeat(_ctx: dict[str, Any]) -> str:
    """Trivial job used by health checks and smoke tests."""
    return "ok"


async def send_email(_ctx: dict[str, Any], *, sealed: str) -> None:
    """`sealed` comes from `email.seal_message`, so no PII or secrets appear in job logs."""
    message = email.open_message(sealed)
    await email.send_email(message["to"], message["subject"], message["body"])
