# LegalPro

Backend for a legal services platform. Clients find verified advocates, book and pay for
consultations, and follow their cases. Advocates manage cases, documents, hearings and
teams, and both sides chat in real time.

**The app lives in [`backend/`](backend/).** It's a single FastAPI service (Python 3.13,
PostgreSQL, Redis, S3-compatible storage). Setup, architecture and API notes are in
[`backend/README.md`](backend/README.md).

```bash
cd backend
cp .env.example .env      # then fill in the secrets
docker compose up --build # API docs at http://localhost:8000/docs
```

`backend/docs/history.md` is the original analysis of the earlier Django microservices,
kept for reference only.
