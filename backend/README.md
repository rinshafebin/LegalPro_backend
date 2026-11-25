# Advocate & Client Management API

FastAPI modular monolith that replaces the old Django microservices.

**Stack:** Python 3.13 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) + asyncpg · Alembic ·
PostgreSQL 16 · Redis 7 · SAQ · S3 (SeaweedFS locally) · SQLAdmin

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
uv run saq app.workers.settings.settings   # background worker
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
| `POST /api/v1/auth/email/verify` (+ `/resend`) | Signed link token from the welcome email, bound to the address, 48 h expiry |
| `GET/PATCH /api/v1/users/me` | Current user + profile |

- Access tokens last 15 minutes; refresh tokens last 7 days and are tracked in Redis.
- Every auth endpoint is rate limited per client IP. Login, password reset and MFA are also
  limited per account.
- Client IPs come from uvicorn. It only trusts `X-Forwarded-For` from addresses in the
  `FORWARDED_ALLOW_IPS` env var (default `127.0.0.1`). In production, set that to your proxy's
  IP, never `*`.
- TOTP secrets and queued email payloads are encrypted with `FIELD_ENCRYPTION_KEY`
  (separate from `SECRET_KEY`), see [Key rotation](#key-rotation).
- Queued email payloads are encrypted, so reset codes and addresses never appear in Redis or
  worker logs.

## Background jobs

Jobs run on [SAQ](https://github.com/tobymao/saq) over Redis. App code only calls
`get_job_queue().enqueue(...)`, `.abort(...)` (`app/workers/queue.py`); the SAQ-specific code lives
in `app/workers/queue.py` and `app/workers/settings.py`. Register new jobs in
`settings["functions"]`.

## Advocates, clients and teams

| Endpoint | Who |
|---|---|
| `GET /api/v1/advocates?q=&city=&specialization=&language=&min_experience=&max_experience=&page=&page_size=` | Public. Verified, active advocates only |
| `GET /api/v1/advocates/{id}` | Public. `id` is always the advocate's `User.id` |
| `GET/PATCH /api/v1/advocates/me` | Advocate (profile, languages, fee, specializations by slug) |
| `GET/PATCH /api/v1/clients/me` | Client |
| `GET /api/v1/specializations` · `POST` | Public list · admin create |
| `/api/v1/teams` (CRUD, `/members`) | Verified advocates. Lead manages; members can view and leave; others get 404 |
| `GET /api/v1/admin/advocates?status=pending` · `POST …/{id}/verify` · `/unverify` | Admin. Verify requires the advocate's email to be verified |

## Admin users

Admins can't self-register. Create one with:

```bash
uv run python -m app.scripts.create_admin admin@example.com          # prompts for password
docker compose exec api python -m app.scripts.create_admin admin@example.com
```

## Key rotation

`FIELD_ENCRYPTION_KEY` is a comma-separated list of Fernet keys, newest first. The first key
encrypts; every key can decrypt.

1. Prepend a new key: `FIELD_ENCRYPTION_KEY=<new>,<old>`, then deploy.
2. Re-encrypt stored values: `uv run python -m app.scripts.rotate_field_encryption`
3. After queued jobs and pending MFA setups made with the old key are gone (minutes), remove the
   old key and deploy again.

`SECRET_KEY` signs email-verification links and hashes reset codes. Rotating it only
invalidates outstanding links and codes.

## Bookings & payments

| Endpoint | Who |
|---|---|
| `GET/PUT /api/v1/advocates/me/availability` | Advocate. Weekly windows in the advocate's own timezone |
| `GET /api/v1/advocates/{id}/slots?start_date=&days=` | Public. Free slots in UTC |
| `POST /api/v1/bookings` | Client with verified email |
| `GET /api/v1/bookings`, `GET /bookings/{id}` | The booking's client or advocate (others get 404) |
| `POST /bookings/{id}/cancel` · `/reschedule` · `/complete` | Either party · either party (confirmed only) · advocate after the end time |
| `POST /api/v1/payments/orders` → Razorpay Checkout → `POST /payments/verify` | Client |
| `POST /api/v1/payments/webhook` | Razorpay (signature-authenticated) |

How it works:
- All times are stored in UTC. Availability is local wall-clock time in `advocate_profiles.timezone`
  (default `Asia/Kolkata`). DST gaps are skipped, and slot lengths are real elapsed time.
- Overlapping active bookings (`pending_payment`, `confirmed`) are refused by Postgres exclusion
  constraints (`btree_gist` + `tstzrange`), per advocate and per client.
- Bookings must be at least `BOOKING_MIN_LEAD_MINUTES` ahead and at most `BOOKING_HORIZON_DAYS`
  ahead, and must start exactly on one of the advocate's slots. Only verified, active advocates
  with a fee can be booked.
- The fee is copied onto the booking. The Razorpay order amount comes from that copy and is
  never taken from the client. A fee of 0 confirms immediately.
- Unpaid bookings expire after `BOOKING_PAYMENT_TIMEOUT_MINUTES` (SAQ job
  `booking:{id}:expire`, plus a once-a-minute sweep), which frees the slot.
- Reminders go out 24 h and 1 h before (`booking:{id}:reminder:{kind}:{start_epoch}`). Cancel
  and reschedule abort them, and the reminder job re-checks the booking before sending.
- Webhooks: HMAC signature is checked (400 + log on failure). Each event id is stored, so a
  redelivery changes nothing. Order id, amount and currency must match (otherwise logged and
  acknowledged without effect). A payment that arrives after expiry revives the booking if the
  slot is still free; otherwise it's flagged `refund_status = pending_review`.
- **Refunds are not automated yet.** Cancelling a paid booking flags the payment
  `pending_review`.
