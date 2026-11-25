"""SAQ worker entrypoint: `saq app.workers.settings.settings`."""

from typing import Any

from saq.types import SettingsDict

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
    "functions": [tasks.heartbeat, tasks.send_email],
    "cron_jobs": [],
    "concurrency": 20,
    "startup": startup,
    "shutdown": shutdown,
}
