from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

@shared_task
def send_welcome_email_task(email, full_name):
    try:
        send_mail(
            subject="Welcome to Our Platform",
            message=f"Hello {full_name},\n\nThank you for registering!",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("Failed to send welcome email to %s: %s", email, e)
