"""Plain-text booking emails. Times are shown in the advocate's timezone, labelled."""

from datetime import datetime
from zoneinfo import ZoneInfo


def format_local(moment: datetime, tz_name: str) -> str:
    local = moment.astimezone(ZoneInfo(tz_name))
    return f"{local:%a %d %b %Y, %H:%M} {local.tzname()} ({tz_name})"


def confirmed(recipient: str, other: str, when: str) -> tuple[str, str]:
    return (
        "Consultation confirmed",
        f"Hello {recipient},\n\nYour consultation with {other} is confirmed for {when}.\n",
    )


def cancelled(recipient: str, other: str, when: str, reason: str | None) -> tuple[str, str]:
    extra = f"\nReason: {reason}\n" if reason else ""
    return (
        "Consultation cancelled",
        f"Hello {recipient},\n\nYour consultation with {other} on {when} was cancelled.\n{extra}",
    )


def rescheduled(recipient: str, other: str, when: str) -> tuple[str, str]:
    return (
        "Consultation rescheduled",
        f"Hello {recipient},\n\nYour consultation with {other} has moved to {when}.\n",
    )


def reminder(recipient: str, other: str, when: str, lead: str) -> tuple[str, str]:
    return (
        f"Reminder: consultation in {lead}",
        f"Hello {recipient},\n\nThis is a reminder of your consultation with {other} at {when}.\n",
    )
