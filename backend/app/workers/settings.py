"""SAQ worker entrypoint: `saq app.workers.settings.settings`."""

from typing import Any

from saq import CronJob
from saq.types import SettingsDict

import app.models  # noqa: F401 - jobs query the DB: register every mapper
from app.core.logging import configure_logging, get_logger
from app.workers import tasks
from app.workers.queue import create_saq_queue

logger = get_logger(__name__)


async def startup(_ctx: Any) -> None:
    configure_logging()
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
    ],
    # Safety net: expiry jobs normally fire on time; the sweep catches any that were lost.
    "cron_jobs": [CronJob(tasks.expire_stale_bookings, cron="* * * * *", timeout=60)],
    "concurrency": 20,
    "startup": startup,
    "shutdown": shutdown,
}
