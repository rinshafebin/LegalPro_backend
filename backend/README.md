# Advocate & Client Management API

FastAPI modular monolith that replaces the old Django microservices.

**Stack:** Python 3.13 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) + asyncpg · Alembic ·
PostgreSQL 16 · Redis 7 · ARQ · S3/MinIO · SQLAdmin

## Quick start (Docker)

```bash
cp .env.example .env            # then set SECRET_KEY and JWT_SECRET_KEY
docker compose up --build       # API_PORT=8010 docker compose up  if 8000 is taken
```

| URL | What |
|---|---|
| http://localhost:8000/docs | OpenAPI / Swagger |
| http://localhost:8000/health/ready | Readiness (Postgres + Redis) |
| http://localhost:9001 | MinIO console (minioadmin / minioadmin) |
| http://localhost:8025 | Mailpit (catches outgoing email) |

## Local development (without Docker for the app)

Needs Postgres and Redis running locally.

```bash
uv sync                                   # creates .venv from uv.lock
createdb advocate_dev && createdb advocate_test
uv run alembic upgrade head
uv run uvicorn app.main:app --reload      # API
uv run arq app.workers.settings.WorkerSettings   # background worker
```

## Quality checks (same as CI)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests alembic/env.py
uv run pytest
```

Tests use `TEST_DATABASE_URL` (the name must end in `_test`) and `TEST_REDIS_URL` (must not be DB 0).
Each test runs inside a rolled-back transaction.

Install the git hooks once: `uv run pre-commit install`.

## Migrations

```bash
uv run alembic revision --autogenerate -m "add bookings"
uv run alembic upgrade head
```

Register every new module's models in `app/models.py`, or autogenerate won't see them.

## Layout

```
app/
├── main.py              # app factory: middleware, error handlers, routers
├── models.py            # imports all module models (Alembic/SQLAdmin/tests)
├── core/                # config, db, redis, logging, errors, deps, security
├── modules/<feature>/   # models.py · schemas.py · service.py · router.py
└── workers/             # ARQ WorkerSettings + jobs
alembic/                 # migrations
tests/
```

## Error format

Every error response has the same shape:

```json
{"error": {"code": "not_found", "message": "Case not found", "details": null}}
```
