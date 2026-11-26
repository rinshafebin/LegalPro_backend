# Legacy parity: Django services → backend/

Checked on 2026-11-26, before the old services were deleted (their last version is in the
local git tag `pre-cleanup`). Each feature of the five Django services and where it lives
now, or why it was dropped.

| Old feature (service) | Now in backend/ |
|---|---|
| Register client / advocate (user) | `POST /auth/register/client`, `/auth/register/advocate` |
| Login, Google login (user) | `POST /auth/login`, `/auth/google`, plus refresh and logout |
| Enable / verify two-factor (user) | `/auth/mfa/setup`, `/auth/mfa/confirm`, login challenge |
| Client / advocate profile update (user, advocate) | `PATCH /clients/me`, `/advocates/me`, `/users/me` |
| Profile images (user, advocate) | `PUT/DELETE /users/me/avatar`; signed `avatar_url` on profiles and in search |
| Welcome email job (user) | `auth/emails.py`, sent through the encrypted email queue |
| Create advocate profile job (advocate) | Done inside registration |
| Specializations, teams, team detail (advocate) | `/specializations`, `/teams` |
| Advocate dashboard (advocate) | `GET /advocates/me/dashboard` (the old one called a job that didn't exist) |
| Advocate search and detail (client) | `GET /advocates`, `/advocates/{id}` |
| Bookings list / create / detail (client) | `/bookings`, plus slots, payments, cancel, reschedule |
| Cases create / list / detail / update (case, client) | `/cases` |
| Case team, hearing date, documents, notes (case) | `/cases/{id}/members`, `/hearings`, `/documents`, `/notes` |
| New-case, team and case-update notifications (case) | `notify()`: `case.opened`, `case.member_added`, `case.status_changed`, `case.note_added`, `case.document_added`, `case.hearing_changed` |
| Chat rooms list / detail (chat) | `/chat/rooms` |
| Messages list / send, mark read (chat) | `/chat/rooms/{id}/messages`, `/read`, plus attachments |
| Edit / delete message (chat) | `PATCH/DELETE /chat/rooms/{id}/messages/{message_id}` |
| Chat WebSocket, realtime notification job (chat) | `/ws` with single-use tickets; `notify()` |

## Intentionally dropped

| Old feature | Why |
|---|---|
| User-created chat rooms | Rooms now belong to a case or a booking, so who can use them follows from that. Free-form rooms skipped permission checks. |
| Mute a chat room | Per-type email opt-out covers the noisy part (new-message emails are also throttled). Can be added later if users ask for it. |
| Advocate `rating`, `cases_count`, `wins_count` | Never calculated (there's no review system); counts can be derived from cases. |
| Advocate date of birth | Never used anywhere; sensitive personal data. |
| Document `visible_to_advocate` | Advocates on a case always see its documents. |
| Cross-service helper jobs (`get_cases_for_client`, …) | One app now: plain database queries replace them. |
| `backend-services/` | An empty Django starter project; nothing to carry over. |
