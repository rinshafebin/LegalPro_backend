"""ARQ worker entrypoint: `arq app.workers.settings.WorkerSettings`."""

from typing import Any, ClassVar

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


async def heartbeat(_ctx: dict[str, Any]) -> str:
    """Trivial job used by health checks and smoke tests."""
    return "ok"


async def startup(_ctx: dict[str, Any]) -> None:
    configure_logging()
    logger.info("worker_startup")


async def shutdown(_ctx: dict[str, Any]) -> None:
    logger.info("worker_shutdown")


class WorkerSettings:
    functions: ClassVar[list[Any]] = [heartbeat]
    cron_jobs: ClassVar[list[Any]] = []
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 60
