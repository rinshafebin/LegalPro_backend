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

## Testing notes

The test suite builds its schema by running the Alembic migrations, not `create_all`. This
catches migration drift that `alembic check` can't see, such as CHECK constraints on enums.

WebSocket tests use `httpx-ws` (`ASGIWebSocketTransport`), which runs in the same event loop
as the rest of the suite. Use `tests.chat_support.connect()` to open sockets. It gives each
socket its own transport, opened and closed inside the test task as anyio requires. The
`ws_client` fixture points the WebSocket handler's per-message sessions at the test
transaction.
