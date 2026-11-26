# Advocate & Client Management API

FastAPI modular monolith that replaces the old Django microservices.

**Stack:** Python 3.13 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) + asyncpg · Alembic ·
PostgreSQL 16 · Redis 7 · SAQ · S3 (SeaweedFS locally) · SQLAdmin

## Quick start (Docker)

```bash
cp .env.example .env            # then set SECRET_KEY and JWT_SECRET_KEY
docker compose up --build       # if ports are taken: API_PORT=8010 DB_HOST_PORT=5434 docker compose up
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
uv run pytest --cov            # fails below the coverage floor in pyproject.toml
uv export --frozen --format requirements-txt -o /tmp/req.txt \
  && uvx pip-audit -r /tmp/req.txt --require-hashes --disable-pip
```

- **Coverage floor:** started at 93.9%. Raise `fail_under` in `pyproject.toml` when coverage
  goes up; never lower it.
- **CI** runs on pushes to `main` and `develop`, on pull requests, and weekly (to catch newly
  published vulnerabilities).
- **Dependabot** opens weekly PRs against `develop` for Python packages (`uv.lock`), Docker
  images and GitHub Actions.

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

**Admins must use two-factor authentication.**
- The script turns MFA on and prints the authenticator secret once.
- Admin endpoints accept only sessions that passed the TOTP step at login. Access tokens carry
  an `mfa` claim, and refresh keeps it.
- An admin without MFA can still log in, but only to run `/auth/mfa/setup` and `/confirm`.
  Then they log in again.

**Admin API (all under `/api/v1/admin`, all audited):**

| Endpoint | What |
|---|---|
| `GET /users`, `POST /users/{id}/deactivate` (`reason`), `POST /users/{id}/reactivate` | Account management. Deactivating revokes every session and closes sockets |
| `GET /advocates`, `POST /advocates/{id}/verify`, `/unverify`; `POST /specializations` | Advocate vetting |
| `GET /refunds`, `POST /refunds/{id}/retry`, `/mark-handled` | Refunds that need a person |
| `GET /audit-logs` | The audit trail |

- **Audit:** every admin request is audited. Changes get their own entry, and list views are
  recorded as `admin.viewed` with the path and query.
- **No case content:** admins can't read cases, notes, documents or chat. The admin role isn't
  in those access rules.

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
- Rescheduling stops `RESCHEDULE_CUTOFF_HOURS` (24 h) before the start.

### Refund policy

| Situation | Refund |
|---|---|
| Client cancels at least `CANCELLATION_FULL_REFUND_HOURS` (24 h) before the start | Full |
| Client cancels later, or doesn't show up | None (`not_eligible`) |
| Advocate cancels at any time | Full |
| Payment lands after the slot was lost, or on a cancelled booking | Full, automatic |

Refunds run as SAQ job `payment:{id}:refund`, which calls Razorpay and retries with backoff.
After the final failed attempt the status becomes `failed`. `refund.processed` and
`refund.failed` webhooks finish the job, and redeliveries are ignored. Only full refunds are
issued, so a retry can never over-refund. Statuses that need an admin: `failed`,
`pending_review`.

## Cases

| Endpoint | Who |
|---|---|
| `POST /api/v1/cases` | Verified advocate, from one of their confirmed/completed bookings (the client comes from the booking) |
| `GET /api/v1/cases`, `GET/PATCH /cases/{id}` | Participants; only the lead edits; a closed case is read-only until reopened |
| `/cases/{id}/members` | Lead adds/removes verified advocates; members can leave |
| `/cases/{id}/notes` | Participants; `is_private` notes are hidden from the client; authors edit their own |
| `/cases/{id}/documents` (+ `/download`) | Multipart upload; download via short-lived presigned URL |
| `/cases/{id}/hearings` | Advocates on the case schedule/update; 24 h and 1 h reminders to everyone on the case |

- **One access rule:** `app/modules/cases/access.py` → `visible_to(user)`, meaning client, lead
  advocate or member. Every case endpoint goes through it. Everyone else gets 404. Admins are
  deliberately not included, because case content is privileged.
- **Case numbers** come from a Postgres sequence: `CASE-<year>-<000001>`.
- **Documents:**
  - Allowed types are PDF, PNG, JPEG, DOCX and TXT, detected from the file content. The
    client's Content-Type and file extension are ignored.
  - Uploads larger than `CASE_DOCUMENT_MAX_MB` are refused with 413 while streaming.
  - Storage keys are `cases/{case}/{uuid}.{ext}` and never contain the user's filename.
  - Objects are private. Downloads use `attachment` presigned URLs signed for
    `S3_PUBLIC_ENDPOINT_URL`.
  - Advocates choose `visible_to_client`. Anything a client uploads is always visible to them.
- **Emails about cases** carry the case number, the time (for hearings) and a link. They never
  include titles, courts, purposes, notes or document names.

## Profile images

- `PUT /api/v1/users/me/avatar` (multipart `file`) and `DELETE /users/me/avatar`, for clients
  and advocates.
- **Validation:** PNG or JPEG only, detected from the content as for case documents. The limit
  is `AVATAR_MAX_MB` (2 MB) and at most `AVATAR_MAX_PIXELS` per side.
- **Re-encoding:** every image is re-encoded with Pillow. That strips EXIF metadata (including
  GPS) and rejects files that only look like images.
- **Storage and URLs:** objects are private. Profiles, including public advocate search and
  detail, return `avatar_url` as a signed URL valid for `S3_PRESIGN_TTL_SECONDS`. URLs are
  signed locally with one cached client, so a search page doesn't call S3.

## Advocate dashboard

`GET /api/v1/advocates/me/dashboard` returns:
- case counts by status (cases you lead or are a member of)
- scheduled hearings in the next 7 days
- upcoming confirmed bookings
- bookings still waiting for payment
- refunds on your bookings that are `requested`, `failed` or `pending_review`

It uses plain queries with no caching.

## Audit log (admins)

- `GET /api/v1/admin/audit-logs`: filter by `case_id`, `actor_id`, `action`, `since`, `until`.
  Newest first. Admins only.
- **What's recorded:** case created, viewed and status changed; members added and removed;
  documents uploaded, downloaded and deleted; chat messages edited and deleted; refund
  retries and refunds marked handled.
- **Chat edits and deletes** keep the original text in `details.previous_body`. The API shows
  it as "[retained, not shown]"; it can only be read from the database.
- **Each entry holds:** actor, action, case, target id, IP and request id. It never holds case
  content.
- **Append-only:** a Postgres trigger rejects UPDATE, DELETE and TRUNCATE on `audit_logs`.
- **IP source:** the same as rate limiting, so `X-Forwarded-For` counts only when it comes from
  `FORWARDED_ALLOW_IPS`.
- **Atomic:** each entry commits in the same transaction as the change it describes.

## Refunds needing an admin

- `GET /api/v1/admin/refunds`: refunds in `failed` or `pending_review`, oldest first.
- `POST /admin/refunds/{payment_id}/retry`: queues a fresh refund attempt with Razorpay.
- `POST /admin/refunds/{payment_id}/mark-handled` with `{"note": "..."}`: sets the refund to
  `resolved` for refunds handled outside the system. A late webhook won't reopen it.
- **Admin alerts:** every active admin gets an in-app notification and an email when a refund
  fails for the last time, needs review, or a `refund.failed` webhook arrives. The email has
  the payment id and a link, and no client details.

## Chat and realtime

**Connecting:**

1. `POST /api/v1/realtime/ticket` with the Bearer token returns `{ticket, expires_in}`.
   - The ticket is single-use and valid for `WS_TICKET_TTL_SECONDS` (30 s by default).
   - Only its hash is stored, in Redis.
   - It's redacted from logs.
2. Connect to `ws(s)://<host>/api/v1/ws?ticket=...`.
   - The `Origin` must be in `CORS_ORIGINS`.
   - A connection with no `Origin` is refused unless `WS_ALLOW_MISSING_ORIGIN=true`. That
     setting is forbidden in production.
   - A JWT never goes in the URL.

**Frames:**

- **Client to server:**
  - `{"type":"chat.send","ref","room_id","body","client_msg_id?"}`
  - `{"type":"chat.read","ref","room_id","seq"}`
  - `{"type":"ping"}`
- **Server to client:** `ready`, `ack` (echoes `ref`), `error` (`code`, `message`, `ref`),
  `chat.message`, `chat.read`, `notification`, `pong`.
- **Close codes:**
  - `4401`: the session was revoked (logout everywhere, password change or deactivation).
  - `1009`: the frame was too big.
  - `1008`: a policy violation, such as frame flooding.

**Rooms:**

- One room per case is created with the case. One room per booking is created when the
  booking is confirmed (on payment or immediately if free).
- Existing data was backfilled by the migration.

**Access:**

- There's no participant table. `app/modules/chat/access.py` derives access from the case
  (`visible_to`) or the booking (client or advocate, while it's confirmed or completed).
- Access is re-checked on every message, every read receipt and every REST call.
- Recipients for each message are computed from the database when it's sent.
- So a removed member or a cancelled booking loses access on the next message. A closed case
  keeps its history readable but takes no new messages.
- Every frame also re-checks that the account is active and the token version unchanged.

**Messages:**

- **Sender:** always the authenticated user.
- **Limits:**
  - Bodies are 1–4000 characters, enforced in the API and by a DB CHECK.
  - Each user can send `CHAT_RATE_LIMIT_MESSAGES` messages per
    `CHAT_RATE_LIMIT_WINDOW_SECONDS`, across REST and WebSocket.
- **Resends:** resending the same `client_msg_id` returns the original message; it isn't
  stored twice.
- **History:**
  - `GET /chat/rooms/{id}/messages?before=<seq>` pages back through history.
  - `?after=<seq>` catches up after a reconnect.
  - `seq` is a global identity column.
- **Read state:**
  - `POST /chat/rooms/{id}/read` moves the read marker forward only, never past the newest
    message.
  - Unread counts appear in `GET /chat/rooms` and `GET /chat/unread`.
  - `GET /chat/rooms/{id}` returns each participant's `last_read_seq`.
- **Edit and delete:** `PATCH` and `DELETE /chat/rooms/{id}/messages/{message_id}`.
  - Only the sender can do either. Edits are allowed within `CHAT_EDIT_WINDOW_MINUTES` (15)
    and set `edited`.
  - Delete is a soft delete. The message stays as a placeholder (`deleted: true`,
    `body: null`), an attachment's object is kept but no longer served, and the original
    text goes to the audit log.
  - Both are broadcast as `chat.message_updated`.
- **Attachments:** `POST /chat/rooms/{id}/attachments` uses the case document validation
  (type from content, `CHAT_ATTACHMENT_MAX_MB`, private objects, presigned download).

**Fan-out:**

- After commit, each event is published once per recipient to the Redis channel
  `rt:user:<id>`.
- Each API process subscribes only to the users connected to it, so every socket gets each
  event exactly once, whichever worker it's on.
- Each socket has a bounded outbound queue. A client that can't keep up is disconnected
  (`4408`) and catches up via `after=`.
- To try it with two workers:
  `docker compose -f docker-compose.yml -f docker/compose.two-workers.yml up -d`.
- If Redis is down, messages are still saved, and clients pick them up over REST.

## Notifications

- `notify(session, user, type, title=..., data=..., email=(subject, body))` in
  `app/modules/notifications/service.py` is the only way to notify someone. It:
  - stores the notification in-app
  - pushes it over the WebSocket
  - queues the email through the sealed (encrypted) email queue
- **Endpoints:**
  - `GET /api/v1/notifications` (`?unread=true`), `GET /notifications/unread-count`
  - `POST /notifications/{id}/read`, `POST /notifications/read-all`
- **Email opt-out:** `GET/PUT /notifications/preferences` turns email on or off per type.
  In-app notifications always stay on.
- **Types:**
  - Bookings: confirmed, cancelled, rescheduled, reminder
  - Cases: case opened, member added, hearing reminder, status changed, new note (non-private
    only), new document (client-visible only), hearing added or changed. The client is never
    told about private notes or hidden documents.
  - Chat: new messages. At most one per room per recipient per
    `CHAT_NOTIFY_THROTTLE_MINUTES`, and it never contains the message text.
  - Refunds: `refund.attention`, for admins only.

## Account deletion and data export

**Export:** `GET /api/v1/users/me/export` downloads the user's account, profile, bookings,
payments and notification settings as JSON. It's limited to 5 per hour and audited.

**Deletion:** `POST /api/v1/users/me/deletion`.

- **Confirmation:**
  - Password, plus an authenticator code if MFA is on.
  - Google-only accounts without MFA must have logged in within the last 10 minutes.
- **Refused (`409 deletion_blocked`)** while the user still has obligations:
  - an open case they lead
  - an upcoming or unpaid booking
  - a refund that isn't finished

  The response lists them.
- **Admins** can't self-delete; another admin deactivates them.
- **Grace period:** the account is deactivated immediately (sessions revoked, sockets closed)
  and anonymized after `ACCOUNT_DELETION_GRACE_DAYS` (14). A daily sweep catches lost jobs.
  **Logging in during the grace period cancels the deletion.** The user gets an email at each
  step.
- **Wiped:**
  - email, which becomes `deleted-<id>@deleted.invalid` and frees the address
  - password and MFA
  - name (now "Deleted user"), phone, address, bio and profile image
  - notifications and preferences, chat read state, availability, team memberships
- **Kept:** cases and what the user wrote in them, bookings, payments, the audit log, and an
  advocate's Bar Council ID (so retained case records still show who handled them). These are
  records the law may require us to keep. **Retention periods need confirming with a lawyer.**

## Security

- **Headers:** every response gets `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy` and a
  CSP of `default-src 'none'; frame-ancestors 'none'`.
  - Swagger UI and ReDoc get a looser CSP for their CDN assets. They're off in production
    unless `API_DOCS_ENABLED=true`.
  - HSTS is added in production.
- **Hosts:** requests with a `Host` not in `ALLOWED_HOSTS` get 400. The health probes and
  `/metrics` are exempt.
- **Body limits:**
  - JSON bodies: `MAX_JSON_BODY_KB` (1 MB).
  - Multipart: the largest file limit plus 1 MB.
  - Oversized bodies get 413, including chunked uploads without a `Content-Length`.
- **Timeouts:** every outbound call has one: Razorpay, Google token checks and S3
  (`EXTERNAL_*_TIMEOUT_SECONDS`), and SMTP (15 s). Postgres kills statements after
  `DB_STATEMENT_TIMEOUT_MS`.
- **Logs:** never contain passwords, tokens, codes, emails or message text.
  - Values under sensitive keys are replaced.
  - Emails, JWTs, Bearer tokens and `token=`/`code=`/`ticket=` values are masked in every log
    line, including uvicorn's access lines and rendered tracebacks.

## Observability

- **Health:** `GET /health` (liveness) and `GET /health/ready` (checks Postgres and Redis).
- **Metrics:** `GET /metrics` (Prometheus) is off unless `METRICS_TOKEN` is set.
  - Scrapers send `Authorization: Bearer <token>`. Don't route it through the public proxy.
  - Labels use route templates (`/api/v1/cases/{case_id}`), never raw ids.
  - Multiple workers are combined through `PROMETHEUS_MULTIPROC_DIR`.
- **Sentry:** set `SENTRY_DSN` to enable it. It collects no user identity, cookies, bodies,
  query strings or local variables, keeps only an allowlist of headers, and scrubs every event
  string the same way as the logs.

## Production checklist

**Release process:**

1. Run migrations as a separate release step, before starting the new version:
   `alembic upgrade head` (in compose: the `migrate` service). The app never migrates on
   start.
2. Start the API with the image's default command (`docker/start-api.sh`). It runs:
   - `WEB_CONCURRENCY` uvicorn workers (2 by default; about 2 per CPU core)
   - no autoreload
   - `--proxy-headers`
   - a graceful shutdown that closes WebSockets with 1012 and waits up to
     `GRACEFUL_SHUTDOWN_SECONDS` (20)
3. Start the worker: `saq app.workers.settings.settings`. Run one or more; jobs are
   deduplicated.

**Required environment** (the app refuses to start in production if any of these are unsafe):

| Setting | Production value |
|---|---|
| `ENVIRONMENT` | `production` (turns on the strict checks, HSTS and docs-off) |
| `SECRET_KEY`, `JWT_SECRET_KEY` | Random, at least 32 characters, different from each other |
| `FIELD_ENCRYPTION_KEY` | Fernet key(s), different from the above. Back them up: losing them loses MFA secrets |
| `DATABASE_URL`, `REDIS_URL` | Managed Postgres 16 and Redis 7. Size `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` × processes below Postgres `max_connections` |
| `ALLOWED_HOSTS` | The API hostname(s), e.g. `api.example.com` |
| `CORS_ORIGINS`, `FRONTEND_URL` | `https://` origins of the web app only |
| `FORWARDED_ALLOW_IPS` | The reverse proxy / load balancer IP(s), never `*`. Rate limiting and audit IPs depend on it |
| `S3_*` | Bucket, keys and region. `S3_PUBLIC_ENDPOINT_URL` only if browsers use a different host. Keep the bucket private |
| `SMTP_*`, `EMAIL_FROM` | A real provider (SES, Postmark, …) with SPF/DKIM set up for the sending domain |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | **Live** keys (`rzp_live_…`) |
| `RAZORPAY_WEBHOOK_SECRET` | From the Razorpay dashboard webhook for `https://<api-host>/api/v1/payments/webhook`. Events: `payment.captured`, `order.paid`, `payment.failed`, `refund.processed`, `refund.failed` |
| `GOOGLE_CLIENT_ID` | The production OAuth client |
| `SENTRY_DSN`, `METRICS_TOKEN` | Optional (the token must be at least 32 characters) |

## Testing notes

The test suite builds its schema by running the Alembic migrations, not `create_all`. This
catches migration drift that `alembic check` can't see, such as CHECK constraints on enums.

WebSocket tests use `httpx-ws` (`ASGIWebSocketTransport`), which runs in the same event loop
as the rest of the suite. Use `tests.chat_support.connect()` to open sockets. It gives each
socket its own transport, opened and closed inside the test task as anyio requires. The
`ws_client` fixture points the WebSocket handler's per-message sessions at the test
transaction.
