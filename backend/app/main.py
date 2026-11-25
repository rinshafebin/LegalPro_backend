from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.db import engine
from app.core.errors import ERROR_RESPONSE_SCHEMA, register_exception_handlers
from app.core.logging import RequestIdMiddleware, configure_logging, get_logger
from app.core.redis import close_redis
from app.modules.auth.router import router as auth_router
from app.modules.health.router import router as health_router
from app.modules.users.router import router as users_router
from app.workers.queue import get_job_queue

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("startup", environment=get_settings().environment)
    yield
    await get_job_queue().close()
    await close_redis()
    await engine.dispose()
    logger.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        debug=settings.debug,
        lifespan=lifespan,
        responses={
            400: ERROR_RESPONSE_SCHEMA,
            401: ERROR_RESPONSE_SCHEMA,
            403: ERROR_RESPONSE_SCHEMA,
            404: ERROR_RESPONSE_SCHEMA,
            422: ERROR_RESPONSE_SCHEMA,
            429: ERROR_RESPONSE_SCHEMA,
        },
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(RequestIdMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(users_router, prefix=settings.api_prefix)
    return app


app = create_app()
