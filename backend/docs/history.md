> **Historical document.** Written on 2026-11-25, before the rebuild, as the plan for
> replacing the five Django microservices with the FastAPI app in `backend/`. The old
> services were removed on 2026-11-26 (local git tag `pre-cleanup` has the last version
> that contains them). It describes the old code and the plan, not the current system:
> see [`backend/README.md`](../README.md) for that. What happened to each old feature is
> in [`legacy-parity.md`](legacy-parity.md).

# Legal Platform Backend — Analysis & Completion Guide

> Platform: clients find advocates, book consultations, and track cases; advocates manage teams, cases and chat.
> Status as of 2026-11-25: 5 Django microservices, roughly 3,800 lines. None of them run end-to-end yet.

---

## 1. What exists today

| Service | Port | What it has | State |
|---|---|---|---|
| `user-services` | 8001 | Custom `User` (email + role), Client/Advocate profiles, register, login, Google login, TOTP MFA, welcome email | **Most complete.** Usable base |
| `advocate-service` | 8006 | Advocate profile, specializations, teams, dashboard | Broken: wrong `User` model, calls Celery tasks that don't exist |
| `case-services` | 8002 | Case, team members, documents, notes | **Won't start**: empty `urls.py`, imports missing tasks |
| `client-services` | — | Advocate search, client case list, bookings | Broken: `booking` table never created, bookings not routed, IDOR |
| `chat-service` | 8003 | Rooms, messages, WebSocket consumer | **Won't start**: bad ASGI settings path, auth calls an endpoint that doesn't exist |
| `backend-services` | — | Empty Django 6.0 skeleton with `users/advocates/cases/clients` apps | Just started (untracked) |

### The core architectural problem

All 5 services use the **same Postgres database** and the **same JWT secret**. Each service re-declares the
other services' tables with `managed = False`, and those copies disagree with each other:

| Table | user-services | advocate-service | client-services | chat-service |
|---|---|---|---|---|
| `users` | email, role, no `username` | adds `username` ❌ | id/email/role | `AbstractUser` (username, first_name…) ❌ |
| specialization | `users_specialization` | `advocate_specialization` ❌ | `specialization` ❌ | — |
| `advocate_profile` | managed (creates it) | **also managed**: migration conflict ❌ | read-only copy | — |
| `booking` | — | — | `managed=False`, so **nobody creates it** ❌ | — |

On top of that, services call each other with **Celery RPC** (`send_task(...).get()` inside HTTP requests).
That blocks the request, needs a worker on the other side, and two of the called tasks
(`user_service.tasks.get_user_info`, `case_service.tasks.get_advocate_dashboard`) don't exist.

**This is a "distributed monolith": you pay the cost of microservices and get none of the benefits.**
The right move is to merge everything into **one backend** (a "modular monolith"), which is what
`backend-services/` was started for.

---

## 2. Decision: Django or FastAPI?

**Recommendation: stay on Django (5.2 LTS) + DRF and merge into `backend-services/`.**

| | Django (recommended) | FastAPI |
|---|---|---|
| Reuse of existing code | ~70% (models, serializers, views move over almost as they are) | ~10% (logic only, all plumbing rewritten) |
| Admin panel (advocate verification, case oversight) | Free (`django.contrib.admin`) | Build it or add SQLAdmin |
| Auth / JWT / MFA | simplejwt + existing code | Write yourself (pyjwt + passlib) |
| WebSockets (chat) | Django Channels (already used) | Native, plus Redis pub/sub |
| Migrations | Built in | Alembic |
| Time to a working MVP | **~3–4 weeks** | ~6–8 weeks |

Choose FastAPI only if learning FastAPI is itself the goal (portfolio or job target). If you do,
follow [Appendix A](#appendix-a--fastapi-variant). The phases below are the same either way.

**Why Django 5.2 and not 6.0:** the `backend-services` skeleton was generated with Django 6.0.1, but
5.2 is the LTS release (supported until April 2028), the existing code targets it, and DRF, simplejwt,
channels and celery-beat are all proven on it. Recreate the venv with `Django>=5.2,<5.3`.

---

## 3. Bugs found (fix these while migrating, not in the old services)

### Blockers (stop the services from starting or working at all)
1. `case-services/cases/urls.py` is empty, but `case_service/urls.py` includes it, so the server fails to start.
2. `case-services/cases/views.py:16-22` imports `notify_hearing_date_update` and `notify_new_note`, which don't exist (ImportError).
3. `case-services/cases/views.py:77` calls `notify_case_update.delay(id)`, but the task takes 2 arguments.
4. `case-services/cases/views.py:90`: `CaseTeamAddView` passes `case`, but the serializer has no `case` field (IntegrityError).
5. `chat-service/chat_service/asgi.py:7` sets `DJANGO_SETTINGS_MODULE = 'your_project_name.settings'`.
6. `chat-service/chat/utils/auth.py` calls `USER_SERVICE_URL/auth/validate-token/`, which user-service doesn't have, so every chat REST call returns 403.
7. `chat-service/chat/views.py:23`: `Participant.objects.create(chatroom=..., role="creator")`. The field is `room`, and `creator` isn't a valid role.
8. `advocate-service/advocate_service/celery.py` points at `user_service.settings` (a copy-paste error).
9. `client-services/client_service/urls.py` never includes `bookings.urls`.
10. No migrations exist for cases, advocates, chat or clients.

### Security
11. **IDOR**: client-services reads `client_id` from the query string or body (`?client_id=5`), so any logged-in user can read anyone's cases and bookings. Always use `request.user.id`.
12. **No auth at all** on any case-services endpoint: anyone can create, edit or read cases.
13. **WebSocket impersonation**: `chat/consumers.py` takes `sender_id` from the client message and never authenticates the socket.
14. **MFA brute force**: `VerifyMFAView` is `AllowAny`, takes a raw `user_id` and has no rate limit. Return a short-lived signed `mfa_token` instead of `user_id`, and throttle.
15. **MFA lockout**: `EnableMFAView` sets `mfa_enabled=True` before the user proves they scanned the QR code. Split it into `setup` and `confirm`.
16. `chat/permissions.py` prints raw JWTs to stdout.
17. Every service: `DEBUG=True`, `CORS_ALLOW_ALL_ORIGINS=True` with credentials. `backend-services` has a hard-coded `SECRET_KEY`.

### Logic and quality
18. Chat messages are broadcast **twice**: once by the consumer, and again by the `post_save` signal through a Celery task.
19. `MarkMessageReadView` exists but isn't routed. The `notifications` app is empty.
20. Cases store `client_id` and `advocate_id` as plain `IntegerField`, with no FK and no integrity checks. Advocate search returns the **profile id**, but cases and bookings expect the **user id**.
21. Bookings: no double-booking guard, no status transitions, no advocate-side endpoints. `razorpay` is installed but not used.
22. `users/tests/test_auth.py` is out of date (uses `username`, `send_welcome_email`, `emit_user_created_event`, URL name `client-register`), so it will fail.
23. Forgot/reset password was removed in the current diff, but the `OTP` model is still there. No refresh-token endpoint, no logout or blacklist.
24. The MFA issuer is `"YourAppName"` in views but `"CaseBridge"` in `utils.py`. `generate_totp_qr` is never used.
25. No `MEDIA_ROOT`/`MEDIA_URL` anywhere, even though `ImageField` and `FileField` are used.
26. Dockerfiles use Python 3.10 with `runserver` while the venvs are 3.13. No `docker-compose.yml`, even though `.env` expects host `db`.
27. RabbitMQ is the broker, but Redis is already required for Channels. Run one service, not two.

---

## 4. Target architecture

```
                ┌──────────────┐
  React/Mobile ─►    Nginx      │──► /api/*  ─► Django (ASGI: uvicorn/daphne)
                │  (TLS, static)│──► /ws/*   ─►   ├─ REST (DRF)
                └──────────────┘                 └─ WebSockets (Channels)
                                                        │
                       ┌────────────────────────────────┼──────────────┐
                       ▼                                ▼              ▼
                  PostgreSQL                         Redis        Celery worker + beat
                 (one schema)              (cache, channel layer,  (emails, reminders,
                                               Celery broker)       payment webhooks)
```

### Folder layout for `backend-services/`

```
backend-services/
├── config/
│   ├── settings/
│   │   ├── base.py          # shared
│   │   ├── dev.py           # DEBUG, console email
│   │   └── prod.py          # security headers, S3, real SMTP
│   ├── urls.py
│   ├── asgi.py              # HTTP + WebSocket router
│   ├── wsgi.py
│   └── celery.py
├── apps/
│   ├── common/              # BaseModel (timestamps), permissions, pagination, exception handler
│   ├── accounts/            # User, OTP, JWT, Google login, MFA, password reset
│   ├── advocates/           # AdvocateProfile, Specialization, Team, verification, public search
│   ├── clients/             # ClientProfile
│   ├── bookings/            # Availability, Booking, Payment (Razorpay)
│   ├── cases/               # Case, CaseMember, Document, Note, Hearing
│   ├── chat/                # Room, Participant, Message, consumer, WS JWT middleware
│   └── notifications/       # Notification model, email + WS push, Celery tasks
├── requirements/
│   ├── base.txt
│   └── dev.txt
├── tests/                   # or a tests/ folder per app
├── Dockerfile
├── docker-compose.yml
├── .env.example             # committed; the real .env is not
└── manage.py
```

### Data model (one source of truth, real foreign keys)

```
User (email, role[client|advocate|admin], is_active, mfa_*)
 ├─1:1─ ClientProfile
 ├─1:1─ AdvocateProfile ──M2M── Specialization
 │        └── AdvocateAvailability (weekday, start, end, slot_minutes)
 ├── AdvocateTeam (lead=User) ──< TeamMember (user=User)
 ├── Booking (client=User, advocate=User, start_at, end_at, status, fee)
 │        └─1:1─ Payment (razorpay_order_id, payment_id, status)
 ├── Case (client=User, lead_advocate=User, booking=Booking?, case_number auto)
 │        ├──< CaseMember (user, role[lead|junior|reviewer])
 │        ├──< CaseDocument (file, uploaded_by, visible_to_client)
 │        ├──< CaseNote (author, text, is_private)
 │        └──< Hearing (date, court, notes)
 ├── ChatRoom (type[direct|case], case=Case?) ──< Participant ──< Message
 └── Notification (user, type, payload JSON, read_at)
```

Rule: **always use FKs to `User`** (`settings.AUTH_USER_MODEL`), never `IntegerField` ids.

---

## 5. Step-by-step plan

### Phase 0 — Prepare (½ day)
```bash
cd ~/Documents/Projects/SingleProject
git checkout -b feat/monolith
git add user-services backend-services && git commit -m "WIP before monolith merge"
# If the shared DB has data you care about:
pg_dump -h localhost -U <user> <db_name> > backup_$(date +%F).sql
```
- Decide whether the existing DB data matters. If it's only test data, **start with a fresh database**. That is much simpler.

### Phase 1 — Foundation (1–2 days)
1. Recreate the venv:
   ```bash
   cd backend-services
   rm -rf venv && python3.13 -m venv venv && source venv/bin/activate
   pip install "Django>=5.2,<5.3" djangorestframework djangorestframework-simplejwt \
     django-cors-headers python-decouple "psycopg[binary]" redis celery django-celery-beat \
     channels channels-redis "uvicorn[standard]" drf-spectacular django-filter pillow \
     pyotp qrcode google-auth razorpay
   pip install pytest pytest-django factory-boy ruff   # dev
   pip freeze > requirements/base.txt
   ```
2. Split settings into `base/dev/prod`. Read **everything** secret from `.env` via `decouple`.
   Create `.env.example` with the names only:
   ```
   SECRET_KEY=
   DEBUG=True
   ALLOWED_HOSTS=localhost,127.0.0.1
   DATABASE_URL=postgres://app:app@db:5432/legal
   REDIS_URL=redis://redis:6379/0
   JWT_SIGNING_KEY=
   GOOGLE_CLIENT_ID=
   EMAIL_HOST_USER=
   EMAIL_HOST_PASSWORD=
   RAZORPAY_KEY_ID=
   RAZORPAY_KEY_SECRET=
   CORS_ALLOWED_ORIGINS=http://localhost:5173
   ```
3. Move apps under `apps/` and set `name = "apps.accounts"` etc. in each `apps.py`.
4. Set **`AUTH_USER_MODEL = "accounts.User"` before the first `migrate`**. Changing it later is painful.
5. Add `docker-compose.yml`:
   ```yaml
   services:
     db:     { image: postgres:16, environment: { POSTGRES_DB: legal, POSTGRES_USER: app, POSTGRES_PASSWORD: app }, volumes: [pgdata:/var/lib/postgresql/data], ports: ["5432:5432"] }
     redis:  { image: redis:7, ports: ["6379:6379"] }
     web:    { build: ., command: uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload, env_file: .env, ports: ["8000:8000"], depends_on: [db, redis], volumes: [".:/app"] }
     worker: { build: ., command: celery -A config worker -l info, env_file: .env, depends_on: [db, redis] }
     beat:   { build: ., command: celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler, env_file: .env, depends_on: [db, redis] }
   volumes: { pgdata: {} }
   ```
6. `config/celery.py`: one app, `config_from_object("django.conf:settings", namespace="CELERY")`,
   `CELERY_BROKER_URL = REDIS_URL`. **No RabbitMQ.**
7. `apps/common`: `TimeStampedModel`, a custom DRF exception handler that gives one error shape,
   `PageNumberPagination` as the default, and permissions `IsClient`, `IsAdvocate`, `IsAdminRole`.
8. Add drf-spectacular: `/api/schema/` and `/api/docs/` (Swagger). Your frontend now has live docs.

✅ Done when `docker compose up` gives you a running `/admin/` and `/api/docs/`.

### Phase 2 — Accounts (2–3 days)
Port `user-services/users`. It is the best code in the repo. Move `AdvocateProfile` and `Specialization`
**out** of accounts into `advocates`, and `ClientProfile` into `clients`.

| Endpoint | Notes |
|---|---|
| `POST /api/auth/register/client/` | create user + ClientProfile in `transaction.atomic()` |
| `POST /api/auth/register/advocate/` | creates an AdvocateProfile with `is_verified=False` |
| `POST /api/auth/login/` | returns tokens, or `{mfa_required, mfa_token}` (a signed token valid for 5 min) |
| `POST /api/auth/login/google/` | existing logic |
| `POST /api/auth/token/refresh/` | simplejwt `TokenRefreshView` |
| `POST /api/auth/logout/` | blacklist the refresh token (`rest_framework_simplejwt.token_blacklist`) |
| `POST /api/auth/mfa/setup/` | generate secret + QR (use `utils.generate_totp_qr`), **not yet enabled** |
| `POST /api/auth/mfa/confirm/` | verify the first code, then set `mfa_enabled=True` |
| `POST /api/auth/mfa/verify/` | takes `mfa_token` + `otp`, **throttled** (`ScopedRateThrottle`, 5/min) |
| `POST /api/auth/password/forgot/` · `/reset/` | OTP by email; store a **hash** of the code, 5-min expiry, single use |
| `GET/PATCH /api/auth/me/` | current user + profile |

- Add `role` to the JWT with a custom `TokenObtainPairSerializer.get_token()`.
- Throttle login and register too.
- Fix `test_auth.py` so it matches the real API.

### Phase 3 — Advocates and clients (2–3 days)
- Port the advocate-service models, but use **real FKs** to `User`. Delete the duplicate `User` model and all the RPC helpers.
- `GET /api/advocates/` is public search (merged from client-services `get_advocates`):
  filters `name`, `city`, `specialization`, `min_experience`, `language`, plus pagination. Only `is_verified=True`.
  Use `select_related("user").prefetch_related("specializations")`.
- `GET /api/advocates/{id}/`: public profile. **Decide one id** (use the `User.id`) and use it everywhere.
- `GET/PATCH /api/advocates/me/`: the advocate's own profile.
- Teams: `/api/advocates/teams/` CRUD (existing logic, ORM instead of RPC).
- Admin: `AdvocateProfileAdmin` with a "Verify selected" action, which emails the advocate.
- `GET /api/advocates/me/dashboard/`: counts of active, pending and closed cases, upcoming hearings, upcoming bookings. Plain ORM aggregates.

### Phase 4 — Bookings and payments (3–4 days)
- `AdvocateAvailability` (weekly slots) and `GET /api/advocates/{id}/slots/?date=` (free slots).
- `Booking`: `client`, `advocate`, `start_at`, `end_at`, `status`, `fee`.
  - DB constraint: `UniqueConstraint(fields=["advocate","start_at"], condition=Q(status__in=["pending","confirmed"]))`.
  - Status machine: `pending → confirmed → completed`, or `pending/confirmed → cancelled`. Enforce it in one service function.
- Endpoints: client `POST /api/bookings/`, `GET /api/bookings/` (mine, whichever role),
  advocate `POST /api/bookings/{id}/confirm|reject|complete/`, either side `POST /api/bookings/{id}/cancel/`.
- Razorpay: `POST /api/payments/order/` creates the order, `POST /api/payments/verify/` checks the signature,
  `POST /api/payments/webhook/` (CSRF-exempt, verify `X-Razorpay-Signature`) marks the payment paid and confirms the booking.
- Celery: confirmation emails, plus a beat task that sends reminders 24 h and 1 h before.

### Phase 5 — Cases (3–4 days)
- Port the case-services models with FKs. Generate `case_number` automatically (for example `CB-2026-000123`).
- Object-level permission: a user can see a case only if they are the client, the lead advocate, or a `CaseMember`.
  Put this in one `CaseQuerySet.visible_to(user)` and use it in **every** view.
- Endpoints (a ViewSet with DRF routers):
  - `POST /api/cases/` (advocate only), optionally `from_booking=<id>`
  - `GET /api/cases/`, `GET/PATCH /api/cases/{id}/`
  - `POST/DELETE /api/cases/{id}/members/`
  - `POST /api/cases/{id}/documents/` (multipart, max size, allowed MIME types); the client sees only `visible_to_client`
  - `POST /api/cases/{id}/notes/` (`is_private` notes are hidden from the client)
  - `POST /api/cases/{id}/hearings/`, which schedules reminders
- Media: `MEDIA_ROOT` locally, S3 or Cloudflare R2 (`django-storages`) in production. Serve documents through a permission-checked view or a signed URL, **never publicly**.

### Phase 6 — Chat and notifications (3–4 days)
- WebSocket auth: write `JWTAuthMiddleware` that reads `?token=` (or `Sec-WebSocket-Protocol`), validates it with simplejwt and sets `scope["user"]`.
- Route `ws/chat/<room_uuid>/`. In `connect()`, reject with `close(4403)` if the user isn't a participant.
- In `receive()`: **sender = `scope["user"]`**, save the message, then `group_send` **once**. Delete the `post_save` signal and the Celery broadcast.
- Auto-create a chat room per case (all members) and per confirmed booking (client + advocate).
- REST: room list, paginated message history (cursor pagination), mark-read, unread counts.
- `notifications` app: `Notification` model, `ws/notifications/` personal group `user_<id>`, plus email via Celery.
  A single `notify(user, type, payload)` helper that every other app calls.

### Phase 7 — Quality (ongoing, 2–3 days to catch up)
- Tests with `pytest-django` and `factory_boy`. At minimum: auth flows, the permission matrix (client A can't see client B's case), booking double-booking, and the payment signature.
- `ruff` for lint/format, plus a `pre-commit` hook.
- GitHub Actions: Postgres + Redis services, then `ruff check`, `pytest`.
- Structured logging (no `print`), Sentry in production.

### Phase 8 — Production (1–2 days)
- `prod.py`: `DEBUG=False`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `SECURE_*`, `CSRF_TRUSTED_ORIGINS`.
- Dockerfile: `python:3.13-slim`, non-root user, `uvicorn config.asgi:application --workers 4` (it serves HTTP and WS),
  `collectstatic` + WhiteNoise or Nginx for static files.
- Run migrations as a release step, not at container start.
- Managed Postgres + Redis (Render, Railway, Fly, or AWS RDS/ElastiCache).

### Phase 9 — Clean up
Once every endpoint from the old services is covered and tested, delete `user-services/`, `advocate-service/`,
`case-services/`, `client-services/` and `chat-service/` (git history keeps them). Update the README.

---

## 6. Where each old file goes

| Old | New |
|---|---|
| `user-services/users/models.py` (User, OTP) | `apps/accounts/models.py` |
| `user-services/users/models.py` (ClientProfile) | `apps/clients/models.py` |
| `user-services` + `advocate-service` AdvocateProfile/Specialization | `apps/advocates/models.py` (one copy) |
| `advocate-service/advocates` teams | `apps/advocates/` |
| `client-services/clients/tasks.py` search | `apps/advocates/views.py` (`AdvocateViewSet` + django-filter) |
| `client-services/bookings` | `apps/bookings/` |
| `case-services/cases` | `apps/cases/` |
| `chat-service/chat` | `apps/chat/` |
| All `managed=False` copies, `authentication.py` variants, Celery RPC helpers, `chat/utils/auth.py` | **Delete** |

## 7. Timeline (one developer)

| Week | Phases |
|---|---|
| 1 | 0, 1, 2 (foundation + auth working with tests) |
| 2 | 3, 4 (advocates, search, bookings, payments) |
| 3 | 5, 6 (cases, chat, notifications) |
| 4 | 7, 8, 9 (tests, CI, deploy, cleanup) |

---

## Appendix A — FastAPI variant

Use the same phases, data model and endpoints. Swap the stack:

| Concern | Django | FastAPI |
|---|---|---|
| ORM / migrations | Django ORM | SQLAlchemy 2.0 (async) + Alembic |
| Validation | DRF serializers | Pydantic v2 schemas |
| Auth | simplejwt | `pyjwt` + `pwdlib[argon2]` + an `OAuth2PasswordBearer` dependency (or `fastapi-users`) |
| Permissions | DRF permission classes | Dependencies: `Depends(require_role("advocate"))` |
| Admin | built in | SQLAdmin |
| WebSockets | Channels | native `WebSocket` + Redis pub/sub (`redis.asyncio`) |
| Background jobs | Celery | Celery (same), or `arq` |
| Settings | decouple | `pydantic-settings` |
| Tests | pytest-django | pytest + `httpx.AsyncClient` |
| Server | uvicorn (ASGI) | uvicorn |

Layout:
```
app/
├── main.py              # FastAPI(), include routers, CORS
├── core/                # config.py, security.py, db.py (async engine/session), deps.py
├── models/              # SQLAlchemy models
├── schemas/             # Pydantic models
├── api/v1/              # auth.py, advocates.py, bookings.py, cases.py, chat.py
├── services/            # business logic (booking state machine, notify, payments)
├── workers/             # celery_app.py, tasks.py
└── alembic/
```
Expect about 2x the effort of the Django path, mostly in auth, the admin replacement and migrations.
