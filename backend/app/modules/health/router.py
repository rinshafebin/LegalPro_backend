from collections.abc import Awaitable
from typing import Literal, cast

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.core.deps import RedisDep, SessionDep
from app.core.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool = True
    redis: bool = True


@router.get("/health", response_model=HealthOut, summary="Liveness probe")
async def liveness() -> HealthOut:
    return HealthOut(status="ok")


@router.get(
    "/health/ready",
    response_model=HealthOut,
    summary="Readiness probe (checks Postgres and Redis)",
    responses={503: {"model": HealthOut}},
)
async def readiness(session: SessionDep, redis: RedisDep) -> HealthOut | JSONResponse:
    db_ok = redis_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness_db_failed")
        db_ok = False
    try:
        await cast(Awaitable[bool], redis.ping())
    except Exception:
        logger.exception("readiness_redis_failed")
        redis_ok = False

    result = HealthOut(
        status="ok" if db_ok and redis_ok else "degraded", database=db_ok, redis=redis_ok
    )
    if result.status != "ok":
        return JSONResponse(result.model_dump(), status_code=503)
    return result
