"""Plain-text email templates for the auth flows."""

from app.core.config import get_settings


def welcome(full_name: str) -> tuple[str, str]:
    app = get_settings().app_name
    return (
        f"Welcome to {app}",
        f"Hello {full_name},\n\nThank you for registering with {app}.\n",
    )


def password_reset(code: str) -> tuple[str, str]:
    minutes = get_settings().otp_ttl_seconds // 60
    return (
        "Your password reset code",
        f"Your password reset code is {code}.\n\n"
        f"It expires in {minutes} minutes and can be used once.\n"
        "If you did not request a password reset, you can ignore this email.\n",
    )
