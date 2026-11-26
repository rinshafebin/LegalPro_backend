from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.db import engine
from app.core.errors import ERROR_RESPONSE_SCHEMA, register_exception_handlers
from app.core.logging import RequestIdMiddleware, configure_logging, get_logger
from app.core.metrics import MetricsMiddleware, mark_process_dead
from app.core.metrics import router as metrics_router
from app.core.middleware import (
    BodySizeLimitMiddleware,
    ProbeFriendlyTrustedHostMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.monitoring import init_sentry
from app.core.redis import close_redis
from app.modules.advocates import router as advocates
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.bookings import router as bookings
from app.modules.cases.router import router as cases_router
from app.modules.chat.router import router as chat_router
from app.modules.clients.router import router as clients_router
from app.modules.health.router import router as health_router
from app.modules.notifications.router import router as notifications_router
from app.modules.payments import router as payments
from app.modules.realtime.hub import hub
from app.modules.realtime.router import router as realtime_router
from app.modules.users.admin import router as users_admin_router
from app.modules.users.router import router as users_router
from app.workers.queue import get_job_queue

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("startup", environment=get_settings().environment)
    yield
    # uvicorn has already closed every WebSocket with 1012 (service restart); clients
    # reconnect to another worker and catch up over REST. Tidy up the hub.
    await hub.close()
    mark_process_dead()
    await get_job_queue().close()
    await close_redis()
    await engine.dispose()
    logger.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    init_sentry("api")

    docs = settings.docs_enabled
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
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
    # Added last = runs first: request id, host check, headers, then the body limit.
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(ProbeFriendlyTrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(RequestIdMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(users_router, prefix=settings.api_prefix)
    app.include_router(clients_router, prefix=settings.api_prefix)
    app.include_router(bookings.availability_router, prefix=settings.api_prefix)
    app.include_router(advocates.router, prefix=settings.api_prefix)
    app.include_router(advocates.specializations_router, prefix=settings.api_prefix)
    app.include_router(advocates.teams_router, prefix=settings.api_prefix)
    app.include_router(advocates.admin_router, prefix=settings.api_prefix)
    app.include_router(bookings.router, prefix=settings.api_prefix)
    app.include_router(payments.router, prefix=settings.api_prefix)
    app.include_router(payments.admin_router, prefix=settings.api_prefix)
    app.include_router(cases_router, prefix=settings.api_prefix)
    app.include_router(chat_router, prefix=settings.api_prefix)
    app.include_router(notifications_router, prefix=settings.api_prefix)
    app.include_router(audit_router, prefix=settings.api_prefix)
    app.include_router(users_admin_router, prefix=settings.api_prefix)
    app.include_router(realtime_router, prefix=settings.api_prefix)
    return app


app = create_app()
