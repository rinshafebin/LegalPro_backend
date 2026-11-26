"""SAQ worker entrypoint: `saq app.workers.settings.settings`."""

from typing import Any

from saq import CronJob
from saq.types import SettingsDict

import app.models  # noqa: F401 - jobs query the DB: register every mapper
from app.core.logging import configure_logging, get_logger
from app.core.monitoring import init_sentry
from app.workers import tasks
from app.workers.queue import create_saq_queue

logger = get_logger(__name__)


async def startup(_ctx: Any) -> None:
    configure_logging()
    init_sentry("worker")
    logger.info("worker_startup")


async def shutdown(_ctx: Any) -> None:
    logger.info("worker_shutdown")


settings: SettingsDict[Any] = {
    "queue": create_saq_queue(),
    "functions": [
        tasks.heartbeat,
        tasks.send_email,
        tasks.expire_booking,
        tasks.send_booking_reminder,
        tasks.expire_stale_bookings,
        tasks.issue_refund,
        tasks.send_hearing_reminder,
        tasks.delete_account,
        tasks.purge_due_deletions,
    ],
    # Safety net: expiry jobs normally fire on time; the sweep catches any that were lost.
    "cron_jobs": [
        CronJob(tasks.expire_stale_bookings, cron="* * * * *", timeout=60),
        # Safety net for account deletions whose job was lost (03:15 UTC daily).
        CronJob(tasks.purge_due_deletions, cron="15 3 * * *", timeout=600),
    ],
    "concurrency": 20,
    "startup": startup,
    "shutdown": shutdown,
}
