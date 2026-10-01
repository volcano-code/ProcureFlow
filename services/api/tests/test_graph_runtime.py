"""Actual LangGraph execution safety, with synthetic data and offline transports only."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Lock
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import get_debug, set_debug
from langchain_core.runnables.config import set_config_context
from langchain_core.tracers.context import collect_runs, tracing_v2_enabled
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context

from procureflow import agent as agent_module
from procureflow.agent import LANGGRAPH_VERSION, RUNTIME, ReadOnlyAgent
from procureflow.errors import DomainError


PRIVATE_REASONING = "SYNTHETIC_PRIVATE_REASONING_DO_NOT_PERSIST"
PRIVATE_EVIDENCE = "SYNTHETIC_RAW_EVIDENCE_DO_NOT_TRACE"
PRIVATE_ARGUMENT = "SYNTHETIC_DOCUMENT_ARGUMENT_DO_NOT_TRACE"
SYNTHETIC_KEY = "synthetic-graph-runtime-test-key"


def tool(name="get_comparison", arguments=None, ident="same-call-id"):
    return {"id": ident, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {})}}


def final(evidence_ids=()):
    return {"content": json.dumps({"summary": "只读测试说明。", "evidence_ids": list(evidence_ids)})}


def completion(message):
    return httpx.Response(200, json={"choices": [{"message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})


@contextmanager
def scripted_agent(messages, **options):
    sent = []
    script = iter(messages)

    def handle(request):
        sent.append(json.loads(request.content))
        return completion(next(script))

    agent = ReadOnlyAgent("https://model.test", SYNTHETIC_KEY, "offline-graph-fixture",
                         transport=httpx.MockTransport(handle), **options)
    try:
        yield agent, sent
    finally:
        agent.client.close()


def private_script():
    return [{"tool_calls": [tool(), tool("get_evidence", {"document_id": PRIVATE_ARGUMENT}, "evidence")],
             "reasoning_content": PRIVATE_REASONING}, final()]


def private_tool(*_args):
    return {"text": PRIVATE_EVIDENCE, "fragments": []}


def assert_no_private_state(value):
    encoded = json.dumps(value, default=str, ensure_ascii=False)
    for sentinel in (PRIVATE_REASONING, PRIVATE_EVIDENCE, PRIVATE_ARGUMENT, SYNTHETIC_KEY):
        assert sentinel not in encoded


def graph_nodes(events):
    return [event["node"] for event in events if event["type"] == "graph_node"]


def test_real_compiled_graph_executes_routes_without_persistent_state(monkeypatch):
    original = CompiledStateGraph.invoke
    invocations = []

    def invoke(graph, inputs, config=None, **kwargs):
        assert graph.checkpointer is False
        assert graph.store is None and graph.cache is None
        assert set(graph.get_graph().nodes) == {"__start__", "model", "tools", "validate", "__end__"}
        assert all(not node.retry_policy for node in graph.nodes.values())
        assert set(inputs) == {"model_call", "tool_count", "route", "done"}
        assert_no_private_state(inputs)
        result = original(graph, inputs, config=config, **kwargs)
        assert set(result) == {"model_call", "tool_count", "route", "done"}
        assert result["done"] is True
        assert_no_private_state(result)
        invocations.append((config, kwargs))
        return result

    monkeypatch.setattr(CompiledStateGraph, "invoke", invoke)
    events = []
    with scripted_agent(private_script()) as (agent, sent):
        output = agent.run(private_tool, set(), observer=events.append)
    assert len(invocations) == 1
    config, _options = invocations[0]
    assert config["recursion_limit"] == 10 and config["callbacks"] == []
    assert "checkpointer" not in config["configurable"]
    assert graph_nodes(events) == ["model", "tools", "model", "validate"]
    assert output["runtime"] == RUNTIME == "langgraph-read-only-v1"
    assert output["runtime_version"] == LANGGRAPH_VERSION
    assert output["model_calls"] == 2 and output["tool_calls"] == 2
    assert output["usage"]["total_tokens"] == 36
    assert output["trace"] == events
    assert {"messages", "message", "completed_tools", "read_evidence_ids", "seen_call_ids", "output"}.isdisjoint(output)
    assert_no_private_state(output)
    assert_no_private_state(events)
    # Provider-specific reasoning is only sent back to that provider for continuation.
    assert sent[1]["messages"][2]["reasoning_content"] == PRIVATE_REASONING
    assert PRIVATE_EVIDENCE in json.dumps(sent[1])


def test_last_permitted_model_call_can_reach_validation():
    events = []
    with scripted_agent([final()], max_model_calls=1, required_tools=()) as (agent, sent):
        output = agent.run(lambda *_: pytest.fail("Unexpected tool"), set(), observer=events.append)
    assert len(sent) == output["model_calls"] == 1
    assert output["tool_calls"] == 0
    assert graph_nodes(events) == ["model", "validate"]


def test_model_budget_stops_graph_cycle_without_extra_provider_call():
    events, invoked = [], []
    messages = [{"tool_calls": [tool(ident=f"call-{index}")]} for index in range(3)]
    with scripted_agent(messages, max_model_calls=2) as (agent, sent):
        with pytest.raises(DomainError) as error:
            agent.run(lambda *args: invoked.append(args) or {}, set(), observer=events.append)
    assert error.value.code == "BUDGET_EXCEEDED"
    assert len(sent) == len(invoked) == 2
    assert graph_nodes(events) == ["model", "tools", "model", "tools"]


def test_tool_budget_rejects_entire_oversized_batch_before_any_tool():
    events, invoked = [], []
    with scripted_agent([{"tool_calls": [tool(), tool("search_policy", ident="second")]}],
                        max_tool_calls=1) as (agent, sent):
        with pytest.raises(DomainError) as error:
            agent.run(lambda *args: invoked.append(args), set(), observer=events.append)
    assert error.value.code == "BUDGET_EXCEEDED"
    assert len(sent) == 1 and invoked == []
    assert graph_nodes(events) == ["model", "tools"]
    assert not any(event["type"] == "tool_started" for event in events)


@pytest.mark.parametrize("failure", ["timeout", "http"])
def test_failed_model_node_is_never_retried(failure):
    calls, events = [], []

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC_UNSAFE_PROVIDER_ERROR", request=request)
        return httpx.Response(503, text="SYNTHETIC_UNSAFE_PROVIDER_ERROR")

    agent = ReadOnlyAgent("https://model.test", SYNTHETIC_KEY, "offline-graph-fixture",
                         transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(DomainError) as error:
            agent.run(lambda *_: pytest.fail("Unexpected tool"), set(), observer=events.append)
    finally:
        agent.client.close()
    assert error.value.code == "MODEL_CALL_FAILED"
    assert len(calls) == 1 and graph_nodes(events) == ["model"]
    assert "SYNTHETIC_UNSAFE_PROVIDER_ERROR" not in str(error.value)
    assert_no_private_state(events)


def test_failed_tool_node_is_never_retried_or_followed_by_model():
    events, invoked = [], []

    def fail(*args):
        invoked.append(args)
        raise RuntimeError("SYNTHETIC_UNSAFE_TOOL_ERROR")

    with scripted_agent([{"tool_calls": [tool(), tool("search_policy", ident="not-run")]}]) as (agent, sent):
        with pytest.raises(DomainError) as error:
            agent.run(fail, set(), observer=events.append)
    assert error.value.code == "TOOL_CALL_FAILED"
    assert len(sent) == len(invoked) == 1
    assert graph_nodes(events) == ["model", "tools"]
    assert not any(event["type"] == "tool_completed" for event in events)
    assert "SYNTHETIC_UNSAFE_TOOL_ERROR" not in str(error.value)


def test_failed_validation_node_is_terminal():
    events = []
    with scripted_agent([{"tool_calls": [tool()]}, {"content": "not a narrative"}, final()]) as (agent, sent):
        with pytest.raises(DomainError) as error:
            agent.run(lambda *_: {}, set(), observer=events.append)
    assert error.value.code == "MODEL_SCHEMA_INVALID"
    assert len(sent) == 2 and graph_nodes(events) == ["model", "tools", "model", "validate"]


def test_actual_graph_recursion_limit_becomes_safe_budget_error(monkeypatch):
    original = CompiledStateGraph.invoke
    invoked, events = [], []

    def invoke(graph, inputs, config=None, **kwargs):
        return original(graph, inputs, config={**config, "recursion_limit": 1}, **kwargs)

    monkeypatch.setattr(CompiledStateGraph, "invoke", invoke)
    with scripted_agent([{"tool_calls": [tool()]}]) as (agent, sent):
        with pytest.raises(DomainError) as error:
            agent.run(lambda *args: invoked.append(args), set(), observer=events.append)
    assert error.value.code == "BUDGET_EXCEEDED"
    assert len(sent) == 1 and invoked == []
    assert graph_nodes(events) == ["model"]
    assert "Graph step budget" in str(error.value)


@pytest.mark.parametrize("expiry_stage", ["model", "tool"])
def test_deadline_expiry_stops_graph_before_next_node(monkeypatch, expiry_stage):
    # Only replace the adapter's clock, not Python's shared time module or HTTP clocks.
    clock = [time.monotonic()]
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls, invoked, events = [], [], []

    def handle(request):
        calls.append(request)
        if expiry_stage == "model":
            clock[0] += 3
        return completion({"tool_calls": [tool()]})

    def invoke(*args):
        invoked.append(args)
        clock[0] += 3
        return {}

    agent = ReadOnlyAgent("https://model.test", SYNTHETIC_KEY, "offline-graph-fixture", max_wall_seconds=2,
                         transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(DomainError) as error:
            agent.run(invoke, set(), observer=events.append)
    finally:
        agent.client.close()
    assert error.value.code == "BUDGET_EXCEEDED" and len(calls) == 1
    assert len(invoked) == (expiry_stage == "tool")
    assert "validate" not in graph_nodes(events)
    assert not any(event["type"] == "tool_completed" for event in events)


def test_reused_adapter_starts_fresh_state_and_call_ids_each_run():
    messages = [{"tool_calls": [tool()], "reasoning_content": PRIVATE_REASONING}, final(),
                {"tool_calls": [tool()]}, final()]
    with scripted_agent(messages) as (agent, sent):
        first = agent.run(lambda *_: {"text": PRIVATE_EVIDENCE}, set())
        second = agent.run(lambda *_: {}, set())
    assert len(sent) == 4
    assert first["model_calls"] == second["model_calls"] == 2
    assert first["tool_calls"] == second["tool_calls"] == 1
    assert first["trace"] is not second["trace"]
    assert len(sent[2]["messages"]) == 2
    assert_no_private_state(sent[2:])


def test_concurrent_runs_do_not_share_private_closure_state():
    barrier, lock = Barrier(2), Lock()
    continuations = []

    def handle(request):
        body = json.loads(request.content)
        if len(body["messages"]) == 2:
            return completion({"tool_calls": [tool()]})
        data = json.loads(body["messages"][-1]["content"])["data"]
        with lock:
            continuations.append(data)
        return completion(final())

    def run(agent, marker):
        def invoke(*_args):
            barrier.wait(timeout=5)
            return {"session": marker}
        return agent.run(invoke, set())

    agent = ReadOnlyAgent("https://model.test", SYNTHETIC_KEY, "offline-graph-fixture",
                         transport=httpx.MockTransport(handle))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(lambda marker: run(agent, marker), ("alpha", "beta")))
    finally:
        agent.client.close()
    assert sorted(continuations, key=lambda value: value["session"]) == [{"session": "alpha"}, {"session": "beta"}]
    assert all(output["model_calls"] == 2 and output["tool_calls"] == 1 for output in outputs)
    assert outputs[0]["trace"] is not outputs[1]["trace"]


class RecordingTracingClient:
    """No network implementation: any attempted LangSmith write is recorded locally."""

    def __init__(self):
        self.calls = []

    def create_run(self, *args, **kwargs):
        self.calls.append(("create", args, kwargs))

    def update_run(self, *args, **kwargs):
        self.calls.append(("update", args, kwargs))


@pytest.mark.parametrize("ambient", ["langsmith", "langchain"])
def test_ambient_tracing_does_not_write_to_langsmith(monkeypatch, ambient):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-unused-langsmith-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://tracing.invalid")
    client = RecordingTracingClient()
    # Even a regression that constructs an ambient SDK client cannot make a remote write.
    from langsmith import Client
    monkeypatch.setattr(Client, "create_run", lambda _self, *args, **kwargs: client.create_run(*args, **kwargs))
    monkeypatch.setattr(Client, "update_run", lambda _self, *args, **kwargs: client.update_run(*args, **kwargs))
    context = (tracing_context(enabled=True, client=client) if ambient == "langsmith"
               else tracing_v2_enabled(client=client))
    with context, scripted_agent(private_script()) as (agent, _sent):
        output = agent.run(private_tool, set())
    assert client.calls == []
    assert_no_private_state(output)


def test_ambient_run_collector_cannot_capture_graph_private_state():
    with collect_runs() as collector, scripted_agent(private_script()) as (agent, _sent):
        output = agent.run(private_tool, set())
    # Isolation must cover ambient callback hooks, not only explicit callbacks=[].
    assert collector.traced_runs == []
    assert_no_private_state(output)


def test_ambient_runnable_callbacks_and_config_do_not_receive_graph_state():
    class Recorder(BaseCallbackHandler):
        def __init__(self):
            self.inputs = []

        def on_chain_start(self, _serialized, inputs, **_kwargs):
            self.inputs.append(inputs)

    recorder = Recorder()
    with set_config_context({"callbacks": [recorder], "configurable": {"thread_id": "untrusted-ambient-thread"}}) as ctx:
        with scripted_agent(private_script()) as (agent, _sent):
            output = ctx.run(agent.run, private_tool, set())
    assert recorder.inputs == []
    assert_no_private_state(output)


def test_global_debug_logs_never_contain_private_graph_material(capsys):
    previous = get_debug()
    try:
        set_debug(True)
        with scripted_agent(private_script()) as (agent, _sent):
            output = agent.run(private_tool, set())
    finally:
        set_debug(previous)
    captured = capsys.readouterr()
    assert_no_private_state(captured.out + captured.err)
    assert_no_private_state(output)


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_durable_api_graph_receipts_are_safe_and_never_replayed(system, monkeypatch, outcome):
    from sqlalchemy import select
    from conftest import BUYER, request as create_request
    from procureflow.db import AdviceRunRow, EventRow

    client, service, erp = system
    req = create_request(client)
    before = client.get(f"/api/v1/requests/{req['id']}", headers=BUYER).json()
    created = client.post(f"/api/v1/requests/{req['id']}/advice-runs", headers=BUYER,
        json={"expected_version": before["version"], "idempotency_key": f"graph-{outcome}-receipt"})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    sent, factories = [], []

    def handle(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return completion({"tool_calls": [tool()], "reasoning_content": PRIVATE_REASONING})
        if outcome == "failed":
            raise httpx.ReadTimeout("SYNTHETIC_PROVIDER_FAILURE_DETAIL", request=request)
        return completion(final())

    def factory():
        factories.append(True)
        return ReadOnlyAgent("https://model.test", SYNTHETIC_KEY, "offline-graph-fixture",
                             transport=httpx.MockTransport(handle))

    monkeypatch.setattr(ReadOnlyAgent, "from_env", staticmethod(factory))
    response = client.post(f"/api/v1/advice-runs/{run_id}/process", headers=BUYER)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == outcome.upper()
    if outcome == "completed":
        assert receipt["output"]["runtime"] == RUNTIME
        assert receipt["output"]["runtime_version"] == LANGGRAPH_VERSION
        assert graph_nodes(receipt["output"]["trace"]) == ["model", "tools", "model", "validate"]
    else:
        assert receipt["error_code"] == "MODEL_CALL_FAILED" and receipt["output"] is None
    assert client.get(f"/api/v1/advice-runs/{run_id}", headers=BUYER).json() == receipt
    assert client.post(f"/api/v1/advice-runs/{run_id}/process", headers=BUYER).json() == receipt
    assert len(sent) == 2 and len(factories) == 1
    assert erp.count() == 0
    assert client.get(f"/api/v1/requests/{req['id']}", headers=BUYER).json() == before
    with service.db.transaction() as session:
        tables = (AdviceRunRow.__table__, EventRow.__table__)
        persisted = {table.name: [dict(row) for row in session.execute(select(table)).mappings()] for table in tables}
    assert_no_private_state(persisted)
    assert "SYNTHETIC_PROVIDER_FAILURE_DETAIL" not in json.dumps(persisted, default=str)
    events = client.get(f"/api/v1/requests/{req['id']}/events", headers=BUYER).json()
    assert_no_private_state(events)


@pytest.mark.parametrize("failure_stage, expected_code", [
    ("schema", "MODEL_SCHEMA_INVALID"),
    ("protocol", "MODEL_PROTOCOL_INVALID"),
    ("arguments", "TOOL_ARGUMENTS_INVALID"),
    ("tool", "TOOL_CALL_FAILED"),
    ("domain_tool", "TOOL_SCOPE_DENIED"),
    ("observer", "ADVICE_FAILED"),
])
def test_global_debug_errors_never_include_private_exception_causes(capsys, failure_stage, expected_code):
    messages = {
        "schema": [{"content": PRIVATE_REASONING}],
        "protocol": [{"content": {"private": PRIVATE_REASONING}}],
        "arguments": [{"tool_calls": [tool(arguments={"unexpected": PRIVATE_ARGUMENT})]}],
    }.get(failure_stage, [{"tool_calls": [tool()]}])

    def fail_tool(*_args):
        if failure_stage == "domain_tool":
            raise DomainError("TOOL_SCOPE_DENIED", PRIVATE_EVIDENCE, 403)
        raise RuntimeError(PRIVATE_EVIDENCE)

    def observer(_event):
        if failure_stage == "observer":
            raise RuntimeError(PRIVATE_REASONING)

    previous = get_debug()
    try:
        set_debug(True)
        with scripted_agent(messages) as (agent, _sent), pytest.raises(DomainError) as error:
            agent.run(fail_tool, set(), observer=observer)
    finally:
        set_debug(previous)
    assert error.value.code == expected_code
    captured = capsys.readouterr()
    assert_no_private_state(captured.out + captured.err)


def test_final_narrative_is_returned_but_never_added_to_graph_debug_state(capsys):
    summary = "SYNTHETIC_FINAL_NARRATIVE_FOR_CALLER_ONLY"
    message = {"content": json.dumps({"summary": summary, "evidence_ids": []})}
    previous = get_debug()
    try:
        set_debug(True)
        with scripted_agent([message], required_tools=()) as (agent, _sent):
            output = agent.run(lambda *_: pytest.fail("Unexpected tool"), set())
    finally:
        set_debug(previous)
    assert output["summary"] == summary
    captured = capsys.readouterr()
    assert summary not in captured.out + captured.err
