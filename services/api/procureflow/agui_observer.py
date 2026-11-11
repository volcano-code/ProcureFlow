"""Read-only AG-UI 1.0 observation of a *durable* advice receipt.

This is deliberately NOT an agent execution endpoint. It never reserves, starts,
replays or cancels a provider request and it cannot resume approval/ERP work.
Every HTTP observation is authorized anew; no raw model messages, prompt,
private reasoning, source text or tool arguments are copied from audit rows.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from .errors import DomainError

# A narrowly constrained subset of the official RunAgentInput wire shape.
# SDK compatibility is asserted with the actual 1.0.0 classes in CI.
ID = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]


class ObserveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False)
    threadId: ID
    runId: ID
    messages: list = Field(default_factory=list, max_length=0)
    tools: list = Field(default_factory=list, max_length=0)
    context: list = Field(default_factory=list, max_length=0)
    state: dict = Field(default_factory=dict, max_length=0)
    protocolVersion: Literal["1.0"] = "1.0"

    @model_validator(mode="after")
    def read_only(self):
        # A hostile client cannot provide a previous conversation, tools,
        # forwardedProps, resume payload or a model instruction.
        if self.messages or self.tools or self.context or self.state:
            raise ValueError("Only empty observation state is accepted")
        return self


KNOWN_STATES = frozenset({"PENDING", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED", "STALE"})
OBSERVABLE_TYPES = frozenset({
    "ADVICE_RESERVED", "ADVICE_STARTED", "ADVICE_COMPLETED", "ADVICE_FAILED",
    "ADVICE_STALE", "ADVICE_INTERRUPTED", "AGENT_GRAPH_NODE", "AGENT_TOOL_STARTED",
    "AGENT_TOOL_COMPLETED", "AGENT_TOOL_DENIED", "AGENT_MODEL_ATTEMPT_FAILED",
})
# Whitelisted metadata only: NEVER dump arbitrary audit payloads onto AG-UI.
SAFE_FIELDS = frozenset({"node", "model_call", "tool", "call_id", "duration_ms", "runtime",
                         "code", "http_status", "retrying", "request_may_have_reached_provider"})
TERMINAL = frozenset({"COMPLETED", "FAILED", "INTERRUPTED", "STALE"})


def watch_state(run: dict, cursor: int) -> dict:
    """Small, non-authoritative UI view. Business DB still owns actual state."""
    state = run["status"] if run["status"] in KNOWN_STATES else "INTERRUPTED"
    current = run.get("current") is True
    if state == "COMPLETED":
        action = "review_advice" if current else "recheck_source_then_new_run"
    elif state in ("FAILED", "INTERRUPTED", "STALE") or not current:
        action = "manual_reconcile_no_replay"
    elif state == "PENDING":
        action = "buyer_explicit_start_only"
    else:
        action = "observe_only"
    return {"adviceRunId": run["id"], "requestId": run["request_id"],
            "agentStatus": state, "current": current,
            "staleReason": run.get("stale_reason") if not current else None,
            "errorCode": run.get("error_code"), "humanAction": action,
            "sourceReadOnly": True, "auditCursor": cursor,
            "providerCancellationGuaranteed": False, "externalWritePermitted": False}


def event_projection(row: dict, run_id: str) -> dict | None:
    """One stable, id-addressable CUSTOM event per durable audit row."""
    kind = row.get("type")
    payload = row.get("payload")
    if kind not in OBSERVABLE_TYPES or not isinstance(payload, dict) or payload.get("run_id") != run_id:
        return None
    meta = {key: value for key, value in payload.items()
            if key in SAFE_FIELDS and (value is None or isinstance(value, (str, int, float, bool)))}
    # Limit potentially hostile string values even if a future audit emitter changes.
    meta = {key: value[:128] if isinstance(value, str) else value for key, value in meta.items()}
    return {"type": "CUSTOM", "name": "procureflow.advice.progress",
            "value": {"auditId": row["id"], "kind": kind, "detail": meta}}


def observation_events(run: dict, audit: list[dict], after: int, *, include_message: bool = True):
    """Map a point-in-time snapshot; the *observation* ends, not the model run.

    The returned RUN_FINISHED is the end of the read-only observation request.
    Its result explicitly carries the actual durable agent status, including
    PENDING/RUNNING. A client must not interpret this event as provider success.
    """
    cursor = max([after] + [row["id"] for row in audit])
    yield {"type": "RUN_STARTED", "threadId": run["request_id"], "runId": run["id"],
           "protocolVersion": "1.0", "metadata": {"procureflow": {"mode": "observation_only"}}}, None
    # Every reconnect begins from authoritative state, not a client cache.
    yield {"type": "STATE_SNAPSHOT", "snapshot": watch_state(run, cursor)}, None
    for row in audit:
        event = event_projection(row, run["id"])
        if event is not None:
            yield event, row["id"]
    output = run.get("output")
    if include_message and run["status"] == "COMPLETED" and run.get("current") is True and isinstance(output, dict):
        summary = output.get("summary")
        if isinstance(summary, str) and 0 < len(summary) <= 8000:
            message_id = "summary-" + run["id"]
            yield {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"}, None
            yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": summary}, None
            yield {"type": "TEXT_MESSAGE_END", "messageId": message_id}, None
    yield {"type": "RUN_FINISHED", "threadId": run["request_id"], "runId": run["id"],
           "result": {"mode": "observation_only", "agentStatus": run["status"],
                      "providerSuccessNotImplied": True}}, None


def verify_watch(principal, run_id, payload: ObserveInput, advice, procurement, after: int):
    """All domain reads revalidate pilot membership/session and tenant scope."""
    run = advice.get(principal, run_id)
    if payload.runId != run_id or payload.threadId != run["request_id"]:
        raise DomainError("AGUI_SCOPE_DENIED", "Observation thread or run does not match the receipt", 403)
    audit = procurement.events(principal, run["request_id"], after)
    if len(audit) > 1000:
        raise DomainError("AGUI_LIMIT", "Audit window exceeds limit", 422)
    # A new session check closes an otherwise valid observation if revoked
    # during the database read. No long-lived credential cache is used.
    after_run = advice.get(principal, run_id)
    if after_run["input_hash"] != run["input_hash"] or after_run["status"] != run["status"]:
        # Don't publish an inconsistent mixed snapshot: the next observation
        # can read the new durable state without causing any side effect.
        raise DomainError("AGUI_SNAPSHOT_CHANGED", "Advice changed during observation; read again", 409)
    return after_run, audit


def encode_frames(events):
    """Official SDK validates events and encodes AG-UI SSE; import is optional."""
    from ag_ui.core import Event
    from ag_ui.encoder import EventEncoder
    from pydantic import TypeAdapter
    adapter, encoder = TypeAdapter(Event), EventEncoder()
    for record, cursor in events:
        obj = adapter.validate_python(record)
        wire = encoder.encode(obj)
        # Only durable audit records advance the cursor. A partial frame can
        # always be replayed without skipping a subsequent business record.
        yield (f"id: {cursor}\n" if cursor is not None else "") + wire
