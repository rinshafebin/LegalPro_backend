"""Google ID-token verification (sync google-auth library, run in a thread)."""

from typing import Any

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.concurrency import run_in_threadpool

_transport = google_requests.Request()


def _verify(token: str, client_id: str) -> dict[str, Any]:
    claims: dict[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
        token, _transport, client_id
    )
    return claims


async def verify_id_token(token: str, client_id: str) -> dict[str, Any]:
    """Return the verified claims. Raises ValueError if the token is invalid."""
    return await run_in_threadpool(_verify, token, client_id)
