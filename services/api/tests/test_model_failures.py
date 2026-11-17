"""Deterministic fault injection only: no model network, quality or price claims."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from procureflow import agent as agent_module
from procureflow.agent import ReadOnlyAgent
from procureflow.errors import DomainError
from test_graph_runtime import tool, final

PRIVATE = "SYNTHETIC_SECRET_RESPONSE_AND_REFUSAL"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def completion(message=None, *, usage=USAGE, finish_reason="stop", **choice):
    return httpx.Response(200, json={"choices": [{"message": message or final(),
        "finish_reason": finish_reason, **choice}], "usage": usage})


@contextmanager
def scripted(script, **options):
    seen, invoked, events = [], [], []
    sequence = iter(script)
    def handle(request):
        seen.append(json.loads(request.content))
        result = next(sequence)
        if isinstance(result, Exception):
            raise result
        return result() if callable(result) else result
    agent = ReadOnlyAgent("https://fixture.invalid", "synthetic-unused-key", "offline-fault-fixture",
        required_tools=(), transport=httpx.MockTransport(handle), **options)
    try:
        yield agent, seen, invoked, events
    finally:
        agent.client.close()


def run(agent, invoked, events):
    return agent.run(lambda *args: invoked.append(args) or {}, set(), observer=events.append)


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_pre_dispatch_transient_retry_is_bounded_and_preserves_exact_body(exception):
    with scripted([exception(PRIVATE), completion()], max_model_calls=2) as (agent, seen, invoked, events):
        output = run(agent, invoked, events)
    assert len(seen) == 2 and seen[0] == seen[1] and invoked == []
    assert output["model_calls"] == 1 and output["provider_attempts"] == 2
    assert output["usage"] == USAGE and output["cost"] is None
    retry = next(event for event in events if event["type"] == "model_attempt_failed")
    assert retry["retrying"] is True and retry["request_may_have_reached_provider"] is False
    assert retry["usage"] is None and retry["cost"] is None
    assert PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("limit,retries,expected", [(4, 0, 1), (4, 1, 2), (4, 2, 3), (1, 2, 1), (2, 2, 2)])
def test_connection_retry_never_exceeds_per_call_or_global_budget(limit, retries, expected):
    with scripted([httpx.ConnectError(PRIVATE)] * 4, max_model_calls=limit,
                  max_connection_retries=retries) as (agent, seen, invoked, events):
        with pytest.raises(DomainError) as error:
            run(agent, invoked, events)
    assert error.value.code == "MODEL_CALL_FAILED" and len(seen) == expected and invoked == []
    assert events[-1]["provider_attempts"] == expected
    assert events[-1]["known_usage"] is None and events[-1]["usage"] is None and events[-1]["cost"] is None
    assert PRIVATE not in str(error.value) + json.dumps(events)


def test_retry_consumes_budget_needed_by_next_model_round():
    with scripted([httpx.ConnectError(PRIVATE), completion({"tool_calls": [tool()]}, finish_reason="tool_calls"),
                   completion()], max_model_calls=2) as (agent, seen, invoked, events):
        with pytest.raises(DomainError) as error:
            run(agent, invoked, events)
    assert error.value.code == "BUDGET_EXCEEDED" and len(seen) == 2 and len(invoked) == 1
    assert events[-1]["known_usage"] == USAGE
    assert events[-1]["usage"] is None and events[-1]["usage_complete"] is False


@pytest.mark.parametrize("result,code", [
    (httpx.ReadTimeout(PRIVATE), "MODEL_TIMEOUT"),
    (httpx.WriteTimeout(PRIVATE), "MODEL_TIMEOUT"),
    (httpx.ReadError(PRIVATE), "MODEL_CALL_FAILED"),
    (httpx.WriteError(PRIVATE), "MODEL_CALL_FAILED"),
    (httpx.RemoteProtocolError(PRIVATE), "MODEL_CALL_FAILED"),
    *[(httpx.Response(status, text=PRIVATE), "MODEL_CALL_FAILED") for status in (301, 401, 403, 408, 429, 500, 502, 503, 504)],
])
def test_ambiguous_or_http_failure_is_terminal_even_with_retry_enabled(result, code):
    with scripted([result, completion()], max_connection_retries=2) as (agent, seen, invoked, events):
        with pytest.raises(DomainError) as error:
            run(agent, invoked, events)
    assert error.value.code == code and len(seen) == 1 and invoked == []
    assert events[-1]["type"] == "graph_node_failed" and events[-1]["code"] == code
    assert PRIVATE not in str(error.value) + json.dumps(events)


@pytest.mark.parametrize("response,code", [
    (httpx.Response(200, content=b'{"choices":'), "MODEL_PROTOCOL_INVALID"),
    (httpx.Response(200, content=b'\xff\xff'), "MODEL_PROTOCOL_INVALID"),
    *[(httpx.Response(200, json=value), "MODEL_PROTOCOL_INVALID") for value in
      (None, [], {}, {"choices": None}, {"choices": []}, {"choices": [None]},
       {"choices": [{"message": {"content": "x"}}, {"message": {"content": "x"}}]})],
    (completion(index=True), "MODEL_PROTOCOL_INVALID"),
    (completion(index=2), "MODEL_PROTOCOL_INVALID"),
    (completion(finish_reason="unknown"), "MODEL_PROTOCOL_INVALID"),
    (completion(finish_reason="tool_calls"), "MODEL_PROTOCOL_INVALID"),
    (completion({"tool_calls": [tool()]}), "MODEL_PROTOCOL_INVALID"),
    (completion({"role": "system", **final()}), "MODEL_PROTOCOL_INVALID"),
    (completion({"content": {"private": PRIVATE}}), "MODEL_PROTOCOL_INVALID"),
    (completion(finish_reason="length"), "MODEL_OUTPUT_INCOMPLETE"),
    (completion(finish_reason="content_filter"), "MODEL_REFUSED"),
    (completion({**final(), "refusal": PRIVATE}), "MODEL_REFUSED"),
    (completion({"tool_calls": [tool()], "refusal": PRIVATE}, finish_reason="tool_calls"), "MODEL_REFUSED"),
    (completion({"content": None}), "MODEL_EMPTY_OUTPUT"),
    (completion({"content": "  \n\t"}), "MODEL_EMPTY_OUTPUT"),
    (completion({"content": "Not JSON " + PRIVATE}), "MODEL_SCHEMA_INVALID"),
    (completion({"content": json.dumps({"summary": " ", "evidence_ids": []})}), "MODEL_SCHEMA_INVALID"),
    (completion({"content": json.dumps({"summary": "x", "evidence_ids": "doc:1"})}), "MODEL_SCHEMA_INVALID"),
    (completion({"content": json.dumps({"summary": "x", "evidence_ids": [], "approved": True})}), "MODEL_SCHEMA_INVALID"),
    (completion({"content": json.dumps({"summary": "x", "evidence_ids": ["invented"]})}), "EVIDENCE_NOT_FOUND"),
    (httpx.Response(200, content=b"x" * 200001), "MODEL_RESPONSE_LIMIT"),
    (httpx.Response(200, content=b"{}", headers={"content-length": "200001"}), "MODEL_RESPONSE_LIMIT"),
    (httpx.Response(200, content=b"{}", headers={"content-length": "garbage"}), "MODEL_RESPONSE_LIMIT"),
])
def test_bad_responses_never_run_tools_retry_or_return_prose(response, code):
    with scripted([response, completion()]) as (agent, seen, invoked, events):
        with pytest.raises(DomainError) as error:
            run(agent, invoked, events)
    assert error.value.code == code and len(seen) == 1 and invoked == []
    assert events[-1]["code"] == code and events[-1]["usage"] is None and events[-1]["cost"] is None
    assert PRIVATE not in str(error.value) + json.dumps(events)


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_failed_response_retains_only_allowlisted_reported_usage(reason):
    with scripted([completion(finish_reason=reason)]) as (agent, _, invoked, events):
        with pytest.raises(DomainError):
            run(agent, invoked, events)
    assert events[-1]["known_usage"] == USAGE
    assert events[-1]["usage"] is None and events[-1]["usage_complete"] is False
    assert events[-1]["cost"] is None


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 11}, {"prompt_tokens": False, "completion_tokens": 7}])
def test_missing_or_malformed_usage_stays_unknown_even_after_success(usage):
    with scripted([completion(usage=usage)]) as (agent, _, invoked, events):
        output = run(agent, invoked, events)
    assert output["usage"] is None and output["usage_complete"] is False and output["cost"] is None


class TinyStream(httpx.SyncByteStream):
    def __init__(self, during_read):
        self.during_read, self.reads, self.closed = during_read, 0, False
    def __iter__(self):
        for _ in range(100):
            self.reads += 1
            self.during_read()
            yield b" "
    def close(self):
        self.closed = True


def test_cancellation_between_tiny_chunks_closes_stream_without_waiting_for_8k():
    cancelled = Event()
    stream = TinyStream(cancelled.set)
    with scripted([httpx.Response(200, stream=stream)], cancelled=cancelled.is_set) as (agent, seen, invoked, events):
        with pytest.raises(DomainError) as error:
            run(agent, invoked, events)
    assert error.value.code == "MODEL_CANCELLED" and len(seen) == 1 and invoked == []
    assert stream.reads == 1 and stream.closed


def test_deadline_between_tiny_chunks_closes_stream(monkeypatch):
    from procureflow import outbound
    clock = [time.monotonic()]
    def tick():
        clock[0] += 2
    monotonic = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(agent_module, "time", monotonic)
    monkeypatch.setattr(outbound, "time", monotonic)
    stream = TinyStream(tick)
    with scripted([httpx.Response(200, stream=stream)], max_wall_seconds=1) as (agent, seen, invoked, events):
        with pytest.raises(DomainError) as error:
            run(agent, invoked, events)
    assert error.value.code == "BUDGET_EXCEEDED" and len(seen) == 1 and invoked == []
    assert stream.reads == 1 and stream.closed


@pytest.mark.parametrize("stop", ["before", "during", "retry"])
def test_cooperative_cancellation_prevents_any_next_attempt_or_tool(stop):
    cancelled = Event()
    def respond():
        cancelled.set()
        if stop == "retry":
            raise httpx.ConnectError(PRIVATE)
        return completion({"tool_calls": [tool()]}, finish_reason="tool_calls")
    if stop == "before":
        cancelled.set()
    with scripted([respond, completion()], cancelled=cancelled.is_set) as (agent, seen, invoked, events):
        with pytest.raises(DomainError) as error:
            run(agent, invoked, events)
    assert error.value.code == "MODEL_CANCELLED" and len(seen) == (stop != "before") and invoked == []


def test_retry_backoff_is_inside_wall_budget(monkeypatch):
    clock = [time.monotonic()]
    def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    with scripted([httpx.ConnectError(PRIVATE), completion()], max_wall_seconds=.05) as (agent, seen, invoked, events):
        with pytest.raises(DomainError) as error:
            run(agent, invoked, events)
    assert error.value.code == "BUDGET_EXCEEDED" and len(seen) == 1


@pytest.mark.parametrize("value", [-1, 3, True, 1.5, "1"])
def test_unbounded_connection_retry_settings_rejected(value):
    with pytest.raises(ValueError, match="Connection retries"):
        ReadOnlyAgent("https://fixture.invalid", "synthetic-key", "offline", max_connection_retries=value)


@pytest.mark.parametrize("change", ["request", "policy", "source"])
def test_snapshot_change_during_provider_response_stops_tool_and_future_calls(system, monkeypatch, change):
    from conftest import ready, publish_policy
    from test_advice_runs import create_run, process, edit_request, stored_text
    client, service, erp = system
    req, _quote, _ = ready(client)
    pending = create_run(client, req["id"])
    seen = []
    def handle(request):
        seen.append(request)
        if change == "request":
            edit_request(client, req["id"])
        elif change == "policy":
            publish_policy(client)
        else:
            next(service.document_dir.iterdir()).write_text("SYNTHETIC_CHANGED_SOURCE")
        return completion({"tool_calls": [tool()], "reasoning_content": PRIVATE}, finish_reason="tool_calls")
    monkeypatch.setattr(ReadOnlyAgent, "from_env", staticmethod(lambda: ReadOnlyAgent(
        "https://fixture.invalid", "synthetic-unused-key", "offline", transport=httpx.MockTransport(handle))))
    result = process(client, pending["id"])
    assert result["status"] == "STALE" and not result["current"] and result["output"] is None
    assert len(seen) == 1 and erp.count() == 0 and PRIVATE not in stored_text(service)
    assert process(client, pending["id"]) == result and len(seen) == 1
    events = client.get(f"/api/v1/requests/{req['id']}/events", headers={"Authorization": "Bearer demo-buyer"}).json()
    assert not any(event["type"] == "AGENT_TOOL_STARTED" for event in events)


@pytest.mark.parametrize("fault,code", [(httpx.ReadTimeout(PRIVATE), "MODEL_TIMEOUT"),
    (completion({**final(), "refusal": PRIVATE}), "MODEL_REFUSED"),
    (completion({"content": " "}), "MODEL_EMPTY_OUTPUT"),
    (completion(finish_reason="length"), "MODEL_OUTPUT_INCOMPLETE")])
def test_failure_receipt_is_durable_sanitized_and_never_replayed(system, monkeypatch, fault, code):
    from conftest import request as create_request
    from test_advice_runs import create_run, process, read_run, procurement_state, stored_text
    client, service, erp = system
    req = create_request(client)
    before = procurement_state(service)
    pending = create_run(client, req["id"])
    calls = []
    def handle(request):
        calls.append(request)
        if isinstance(fault, Exception):
            raise fault
        return fault
    monkeypatch.setattr(ReadOnlyAgent, "from_env", staticmethod(lambda: ReadOnlyAgent(
        "https://fixture.invalid", "synthetic-unused-key", "offline", transport=httpx.MockTransport(handle))))
    result = process(client, pending["id"])
    assert result["status"] == "FAILED" and result["error_code"] == code and result["output"] is None
    assert read_run(client, pending["id"]) == process(client, pending["id"]) == result
    assert len(calls) == 1 and erp.count() == 0 and procurement_state(service) == before
    assert PRIVATE not in stored_text(service)
