import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from events import CommunicationEvent, CommunicationEventIn

router = APIRouter(prefix="/events", tags=["Communication Events"])


@router.post("", response_model=CommunicationEvent, summary="Publish a communication event")
async def publish_event(request: Request, body: CommunicationEventIn):
    event = request.app.state.events.add(body)
    await request.app.state.event_queue.put(event)
    return event


@router.get("", response_model=list[CommunicationEvent], summary="List recent communication events")
async def list_events(request: Request, limit: int = Query(100, ge=1, le=250)):
    return request.app.state.events.list(limit=limit)


@router.get("/stream", summary="Stream live communication events as Server-Sent Events")
async def stream_events(request: Request):
    async def event_stream():
        for event in request.app.state.events.list(limit=50):
            yield _format_sse(event)

        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(request.app.state.event_queue.get(), timeout=15)
                yield _format_sse(event)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _format_sse(event: CommunicationEvent) -> str:
    return f"data: {json.dumps(event.model_dump(mode='json'))}\n\n"
