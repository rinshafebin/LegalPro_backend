import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogOut(BaseModel):
    id: int
    occurred_at: datetime
    actor_id: uuid.UUID | None
    actor_email: str | None
    action: str
    case_id: uuid.UUID | None
    case_number: str | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    ip: str | None
    request_id: str | None
    details: dict[str, Any] | None
