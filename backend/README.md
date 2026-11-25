# Advocate & Client Management API

FastAPI modular monolith that replaces the old Django microservices.

**Stack:** Python 3.13 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) + asyncpg · Alembic ·
PostgreSQL 16 · Redis 7 · ARQ · S3 (SeaweedFS locally) · SQLAdmin

## Quick start (Docker)

```bash
cp .env.example .env            # then set SECRET_KEY and JWT_SECRET_KEY
docker compose up --build       # API_PORT=8010 docker compose up  if 8000 is taken
```

| URL | What |
|---|---|
| http://localhost:8000/docs | OpenAPI / Swagger |
| http://localhost:8000/health/ready | Readiness (Postgres + Redis) |
| http://localhost:8333 | S3 API (SeaweedFS; local-only creds in `docker/seaweedfs/s3.json`) |
| http://localhost:8888 | SeaweedFS file browser |
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

## Auth overview

| Endpoint | Notes |
|---|---|
| `POST /api/v1/auth/register/client`, `/register/advocate` | Returns user + tokens. Advocates start unverified. |
| `POST /api/v1/auth/login` | Returns `tokens`, or `{mfa_required: true, mfa_token}` |
| `POST /api/v1/auth/mfa/verify` | `mfa_token` + 6-digit code → tokens. Token is single-use, max 5 attempts. |
| `POST /api/v1/auth/login/google` | Google ID token → tokens (creates a client account on first login) |
| `POST /api/v1/auth/token/refresh` | Rotates the refresh token; reusing an old one revokes all sessions |
| `POST /api/v1/auth/logout`, `/logout/all` | Revoke one refresh token, or every token of the user |
| `POST /api/v1/auth/mfa/setup` → `/mfa/confirm` | MFA only turns on after the first code is confirmed |
| `POST /api/v1/auth/password/forgot` → `/password/reset` | 6-digit code by email, hashed, single-use, 5-minute expiry |
| `GET/PATCH /api/v1/users/me` | Current user + profile |

- Access tokens last 15 minutes; refresh tokens last 7 days and are tracked in Redis.
- Every auth endpoint is rate limited per client IP. Login, password reset and MFA are also
  limited per account.
- Client IPs come from uvicorn. It only trusts `X-Forwarded-For` from addresses in the
  `FORWARDED_ALLOW_IPS` env var (default `127.0.0.1`). In production, set that to your proxy's
  IP, never `*`.
- TOTP secrets are encrypted at rest with a key derived from `SECRET_KEY`. Rotating
  `SECRET_KEY` means re-enrolling MFA.
- Queued email payloads are encrypted, so reset codes and addresses never appear in Redis or
  worker logs.

## Background jobs

App code only calls `get_job_queue().enqueue(...)` (`app/workers/queue.py`). The ARQ-specific
code lives in `app/workers/queue.py` and `app/workers/settings.py`.
