from collections import deque
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class CommunicationEventIn(BaseModel):
    source: str = Field(..., examples=["ari_app", "ht812_api"])
    type: str = Field(..., examples=["dtmf", "route", "hangup"])
    message: str
    channel_id: str | None = None
    caller: str | None = None
    line: str | None = None
    digit: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class CommunicationEvent(CommunicationEventIn):
    id: str
    created_at: datetime


class EventStore:
    def __init__(self, max_events: int = 250) -> None:
        self._events: deque[CommunicationEvent] = deque(maxlen=max_events)

    def add(self, event: CommunicationEventIn) -> CommunicationEvent:
        stored = CommunicationEvent(
            id=str(uuid4()),
            created_at=datetime.now(timezone.utc),
            **event.model_dump(),
        )
        self._events.append(stored)
        return stored

    def list(self, limit: int = 100) -> list[CommunicationEvent]:
        limit = max(1, min(limit, self._events.maxlen or 250))
        return list(self._events)[-limit:]
