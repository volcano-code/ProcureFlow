"""Scripted adapter safety tests, not real-model quality or task evaluations."""
import json
import httpx
import pytest
from procureflow.agent import ReadOnlyAgent
from procureflow.errors import DomainError


def tool(name, arguments=None, ident="c1"):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {})}}


def run_script(messages, invoke, ids):
    sent = []
    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": messages[len(sent) - 1]}],
                                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    agent = ReadOnlyAgent("https://model.test", "synthetic-secret", "scripted-fixture", transport=httpx.MockTransport(handle),
                         require_evidence_reads=True)
    try:
        return agent.run(invoke, ids), sent
    finally:
        agent.client.close()


def final(ids):
    return {"content": json.dumps({"summary": "测试来源解释，不表示语义正确性。", "evidence_ids": ids})}


def evidence(_name, _args):
    return {"fragments": [{"id": "doc:f1", "text": "shipping_cost: unknown"}]}


def test_cited_evidence_must_have_been_read():
    with pytest.raises(DomainError) as exc:
        run_script([{"tool_calls": [tool("get_comparison")]}, final(["doc:f1"])], evidence, {"doc:f1"})
    assert exc.value.code == "EVIDENCE_NOT_READ"


def test_source_available_but_no_citation_fails():
    with pytest.raises(DomainError) as exc:
        run_script([{"tool_calls": [tool("get_comparison")]}, final([])], evidence, {"doc:f1"})
    assert exc.value.code == "EVIDENCE_REQUIRED"


def test_actual_evidence_read_passes_with_semantic_boundary():
    output, sent = run_script([{"tool_calls": [tool("get_comparison"),
        tool("get_evidence", {"document_id": "doc"}, "c2")]}, final(["doc:f1"])], evidence, {"doc:f1"})
    assert output["evidence_read_verified"] is True
    assert output["semantic_factuality_verified"] is False
    assert output["advisory_only"] is True
    assert "Read every cited fragment" in sent[0]["messages"][0]["content"]


def test_read_one_document_does_not_ground_another():
    with pytest.raises(DomainError) as exc:
        run_script([{"tool_calls": [tool("get_comparison"),
            tool("get_evidence", {"document_id": "doc"}, "c2")]}, final(["other:f1"])], evidence, {"doc:f1", "other:f1"})
    assert exc.value.code == "EVIDENCE_NOT_READ"


def test_empty_request_can_be_explained_without_invented_sources():
    output, _ = run_script([{"tool_calls": [tool("get_comparison")]}, final([])], lambda *_: {}, set())
    assert output["evidence_ids"] == [] and output["evidence_read_verified"] is True


def test_reasoning_never_in_output_or_trace():
    output, sent = run_script([{"tool_calls": [tool("get_comparison"),
        tool("get_evidence", {"document_id": "doc"}, "c2")], "reasoning_content": "PRIVATE_REASONING_FIXTURE"},
        final(["doc:f1"])], evidence, {"doc:f1"})
    assert "PRIVATE_REASONING_FIXTURE" not in json.dumps(output)
    assert sent[-1]["messages"][2]["reasoning_content"] == "PRIVATE_REASONING_FIXTURE"


@pytest.mark.parametrize("fragments", [None, {}, [None, {"id": ["doc:f1"]}, {"id": 5}, {"text": "doc:f1"}]])
def test_malformed_fragment_data_cannot_ground_citation(fragments):
    with pytest.raises(DomainError) as exc:
        run_script([{"tool_calls": [tool("get_comparison"),
            tool("get_evidence", {"document_id": "doc"}, "c2")]}, final(["doc:f1"])],
            lambda *_: {"fragments": fragments}, {"doc:f1"})
    assert exc.value.code == "EVIDENCE_NOT_READ"


def test_durable_api_uses_real_adapter_and_persists_source_read_receipt(system, monkeypatch):
    """Real application + adapter code, scripted transport (never a real provider)."""
    from conftest import BUYER, request as create_request, upload
    from procureflow import app as app_module
    client, service, erp = system
    request = create_request(client)
    quote = upload(client, request['id'], 'supplier-b.csv')  # unknown shipping must remain unknown
    calls = []
    fragment_id = next(v['fragment_id'] for v in quote['evidence'].values() if v.get('fragment_id'))
    def handle(req):
        body = json.loads(req.content)
        calls.append(body)
        if len(calls) == 1:
            message = {'tool_calls': [tool('get_comparison')], 'reasoning_content': 'PRIVATE_FIXTURE_REASONING'}
        elif len(calls) == 2:
            comparison = json.loads(body['messages'][-1]['content'])['data']
            assert comparison['quotes'][0]['values']['shipping_cost'] is None
            assert not comparison['quotes'][0]['calculation']['eligible']
            message = {'tool_calls': [tool('get_evidence', {'document_id': quote['document_id']}, 'e2'),
                                      tool('search_policy', {}, 'p3')]}
        else:
            message = {'content': json.dumps({'summary': '运费未知，此报价不能作为最低价推荐。',
                                              'evidence_ids': [fragment_id]})}
        return httpx.Response(200, json={'choices': [{'message': message}],
                                       'usage': {'prompt_tokens': 15, 'completion_tokens': 7}})
    monkeypatch.setattr(app_module.ReadOnlyAgent, 'from_env', lambda: ReadOnlyAgent(
        'https://model.test', 'synthetic-credential', 'scripted-fixture', transport=httpx.MockTransport(handle)))
    current = client.get(f"/api/v1/requests/{request['id']}", headers=BUYER).json()
    created = client.post(f"/api/v1/requests/{request['id']}/advice-runs", headers=BUYER,
                          json={'expected_version': current['version'], 'idempotency_key': 'actual-adapter-1'}).json()
    processed = client.post(f"/api/v1/advice-runs/{created['id']}/process", headers=BUYER)
    assert processed.status_code == 200, processed.text
    result = processed.json()
    assert result['status'] == 'COMPLETED' and result['current']
    assert result['output']['evidence_read_verified']
    assert result['output']['model_calls'] == 3 and result['output']['tool_calls'] == 3
    assert result['output']['usage']['total_tokens'] == 66
    assert result['output']['semantic_factuality_verified'] is False
    assert result['output']['cost'] is None
    assert client.get(f"/api/v1/advice-runs/{created['id']}", headers=BUYER).json() == result
    assert client.post(f"/api/v1/advice-runs/{created['id']}/process", headers=BUYER).json() == result
    assert len(calls) == 3 and erp.count() == 0
    events = client.get(f"/api/v1/requests/{request['id']}/events", headers=BUYER).json()
    saved = json.dumps({'result': result, 'events': events})
    assert 'PRIVATE_FIXTURE_REASONING' not in saved and 'synthetic-credential' not in saved
    assert client.get(f"/api/v1/requests/{request['id']}", headers=BUYER).json() == current
