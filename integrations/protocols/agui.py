"""Opt-in AG-UI read-observation route on the existing authenticated API.

A run here means a bounded observation session, NOT a model/ERP execution.
The existing frontend and internal audit representation remain unchanged.
"""
from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

from ag_ui.core import (CustomEvent, EventType, RunErrorEvent, RunFinishedEvent,
                        RunStartedEvent, StateSnapshotEvent)
from ag_ui.encoder import EventEncoder
from fastapi import Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from procureflow.errors import DomainError

MAX_CURSOR = 9223372036854775807


def cursor_value(after: int, last: str | None) -> int:
    if last is None:
        return after
    if not re.fullmatch(r"0|[1-9][0-9]{0,18}", last) or int(last) > MAX_CURSOR:
        raise DomainError("INVALID_CURSOR", "Last-Event-ID must be a bounded nonnegative integer", 422)
    return max(after, int(last))


def install_agui(app, *, poll_count: int = 20, poll_seconds: float = 0.5):
    if not 1 <= poll_count <= 20 or not 0 <= poll_seconds <= 1:
        raise ValueError("Invalid observation bounds")
    if getattr(app.state, "agui_installed", False):
        raise ValueError("AG-UI routes already installed")
    app.state.agui_installed = True
    service, identities = app.state.service, app.state.identities
    bearer = HTTPBearer(auto_error=False)
    active: set[str] = set()

    def identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise DomainError("UNAUTHENTICATED", "A bearer token is required", 401)
        return identities.authenticate(credentials.credentials)

    @app.get("/api/v1/requests/{request_id}/ag-ui/events", tags=["read-only AG-UI"])
    async def observe(request_id: str, request: Request,
                      after: int = Query(default=0, ge=0, le=MAX_CURSOR),
                      last_event_id: str | None = Header(default=None), principal=Depends(identity)):
        cursor = cursor_value(after, last_event_id)
        # Enforce membership before opening a 200 stream; forbidden scopes never
        # leak a RUN_STARTED event, state, audit IDs or existence metadata.
        await asyncio.to_thread(service.get_request, principal, request_id)
        if len(active) >= 8:
            raise DomainError("OBSERVATION_LIMIT", "Too many observation streams in this process", 429)
        run_id = "observe_" + uuid4().hex
        active.add(run_id)
        encoder = EventEncoder()

        async def generate():
            nonlocal cursor
            previous = None
            try:
                yield encoder.encode(RunStartedEvent(type=EventType.RUN_STARTED, thread_id=request_id, run_id=run_id))
                for _ in range(poll_count):
                    if await request.is_disconnected():
                        return
                    # Both service methods revalidate durable membership/session
                    # in their own transactions; no cached stream authority.
                    current = await asyncio.to_thread(service.get_request, principal, request_id)
                    batch = await asyncio.to_thread(service.events, principal, request_id, cursor)
                    snapshot = {"view_mode": "read_only_observation", "writes_enabled": False,
                        "request": {k: current[k] for k in ("id", "version", "status", "proposal_current") if k in current}}
                    if snapshot != previous:
                        yield encoder.encode(StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=snapshot))
                        previous = snapshot
                    for event in batch[:100]:
                        event_id = event.get("id")
                        if type(event_id) is not int or not cursor < event_id <= MAX_CURSOR:
                            raise ValueError("invalid audit order")
                        # Payloads may contain source text or model data. Only
                        # the explicit metadata allowlist is exported here.
                        value = {"audit_id": event_id, "request_id": request_id,
                                 "kind": event["type"], "created_at": event["created_at"]}
                        cursor = event_id
                        yield f"id: {cursor}\n" + encoder.encode(CustomEvent(type=EventType.CUSTOM, name="procureflow.audit", value=value))
                    if poll_seconds:
                        await asyncio.sleep(poll_seconds)
                yield encoder.encode(RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=request_id,
                    run_id=run_id, result={"observation_only": True, "last_event_id": cursor}))
            except DomainError as error:
                code = "UNAUTHENTICATED" if error.status_code == 401 else "ACCESS_REVOKED"
                yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, code=code, message="Observation stopped; refresh your authorization."))
            except asyncio.CancelledError:
                raise
            except Exception:
                yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, code="OBSERVATION_FAILED", message="Observation stopped."))
            finally:
                active.discard(run_id)

        return StreamingResponse(generate(), media_type=encoder.get_content_type(),
            headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})

    return app


def create_protocol_app():
    from procureflow.app import create_app
    return install_agui(create_app())
