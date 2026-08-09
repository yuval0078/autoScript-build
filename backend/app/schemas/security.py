import uuid
from datetime import datetime

from pydantic import BaseModel


class SecurityEventResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    outcome: str
    actor_user_id: uuid.UUID | None
    subject_user_id: uuid.UUID | None
    username: str | None
    request_id: str | None
    metadata: dict
    created_at: datetime
