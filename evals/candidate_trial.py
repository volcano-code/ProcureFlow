"""Disposable trial worker. Receives PUBLIC task inputs, never gold answers.

Import this module without importing app/runtime code. The parent supplies an
allowlisted environment and a private temporary directory. No ERP port is given.
Only hashed fields, fixed counters, safe errors and source locators are exported.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time
from .candidate_quality import FIELDS, fingerprint, seal_fields

SAFE_ERRORS = frozenset({'MODEL_CALL_FAILED', 'MODEL_PROTOCOL_INVALID', 'MODEL_TIMEOUT',
    'MODEL_SCHEMA_INVALID', 'MODEL_USAGE_REQUIRED', 'MODEL_REFUSED', 'BUDGET_EXCEEDED',
    'MODEL_EMPTY_OUTPUT', 'MODEL_RESPONSE_LIMIT', 'MODEL_OUTPUT_INCOMPLETE',
    'EVIDENCE_NOT_FOUND', 'EVIDENCE_NOT_READ', 'EVIDENCE_REQUIRED', 'TOOL_POLICY_DENIED',
    'NO_RECOGNIZED_FIELDS', 'CANDIDATE_FAILED', 'CANDIDATE_OUTPUT_INVALID',
    'PARSER_TIMEOUT', 'DEPENDENCIES_UNAVAILABLE'})


def empty_record(case, mode, reason='NOT_RUN'):
    return {'id': case['id'], 'source_sha256': case['sha256'], 'mode': mode,
        'status': 'blocked', 'reason': reason, 'fields': {}, 'clarifications': {},
        'side_effects': None, 'provider_requests': 0, 'network_attempted': False,
        'usage': None, 'cost': None, 'tool_path': [], 'elapsed_seconds': 0.0}


def locator(ref, source):
    # Fingerprints, not source prose/model text, are exported to public CI.
    return {'line': ref.get('line'),
            'text_sha256': hashlib.sha256(ref.get('text', '').encode()).hexdigest(),
            'source_sha256': source}


def baseline(case):
    from procureflow.parsers import parse_document
    from procureflow.errors import DomainError
    result = empty_record(case, 'baseline')
    result.update(side_effects=dict(quotes=0, approvals=0, operations=0, erp_attempts=0))
    try:
        parsed = parse_document('synthetic.txt', case['text'].encode(), 'synthetic-doc')
        values = parsed['values']
        evidence = {field: [locator(ref, parsed['sha256'])] for field, ref in parsed['evidence'].items()}
        result['fields'] = seal_fields(values, evidence)
        conflicts = {x.split(':', 1)[1] for x in parsed['issues'] if x.startswith('CONFLICT:')}
        result['clarifications'] = {field: 'conflict' if field in conflicts else 'missing'
            for field, item in result['fields'].items() if not item['known']}
        result.update(status='observed', reason=None)
    except DomainError as error:
        result.update(status='failed', reason=error.code if error.code in SAFE_ERRORS else 'CANDIDATE_FAILED')
    return result


def candidate(case, config):
    import httpx
    from sqlalchemy import select, func
    from procureflow.candidate_agent import CandidateAgent
    from procureflow.candidate_worker import drain_once
    from procureflow.candidates import CandidateService
    from procureflow.candidate_contracts import CandidateReserve, CandidateProcess
    from procureflow.config import Settings
    from procureflow.contracts import Principal, RequestCreate
    from procureflow.db import Database, QuoteRow, ApprovalRow, OperationRow
    from procureflow.service import ProcurementService
    from procureflow.parsers import parse_document
    from procureflow.errors import DomainError

    mode = config['mode']; report = empty_record(case, mode)
    calls = 0; erp_attempts = 0; tools = []; db = None
    transport = None
    class NoERP:
        def __getattr__(self, name):
            nonlocal erp_attempts
            erp_attempts += 1
            raise RuntimeError('EVALUATION_HAS_NO_ERP_PORT')

    def reply(request):
        """Offline protocol fixture only: uses the legacy parser, not gold."""
        body = json.loads(request.content)
        messages = [x for x in body['messages'] if x['role'] == 'tool']
        if not messages:
            message = {'role': 'assistant', 'tool_calls': [
                {'id': 'fixture-request', 'type': 'function', 'function': {'name': 'get_request', 'arguments': '{}'}},
                {'id': 'fixture-source', 'type': 'function', 'function': {'name': 'get_evidence', 'arguments': '{"start":0,"limit":24}'}}]}
        else:
            fragments = next(json.loads(x['content'])['data']['fragments'] for x in messages
                             if 'fragments' in json.loads(x['content']).get('data', {}))
            parsed = parse_document('synthetic.txt', case['text'].encode(), 'fixture')
            by_line = {x['locator']['line']: x['id'] for x in fragments}
            values = {k: parsed['values'][k] for k in FIELDS}
            refs = {k: [by_line[v['line']]] for k, v in parsed['evidence'].items()}
            questions = [{'field': k, 'reason': 'missing', 'question': 'Please supply a source for the missing field.'}
                         for k in FIELDS if values[k] is None or (k == 'tax_mode' and values[k] == 'unknown')]
            message = {'role': 'assistant', 'content': json.dumps({'summary': 'Scripted protocol fixture, not model quality.',
                'values': values, 'references': refs, 'clarifications': questions, 'context_evidence_ids': []})}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop',
            'message': message}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}})

    class CountedTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.MockTransport(reply) if mode == 'fixture' else httpx.HTTPTransport(retries=0, trust_env=False)
        def handle_request(self, request):
            nonlocal calls
            if calls >= config['max_calls']:
                raise DomainError('BUDGET_EXCEEDED', 'Provider request budget reached', 422)
            calls += 1  # Count attempts, including ambiguous failures.
            return self.inner.handle_request(request)
        def close(self):
            self.inner.close()

    try:
        settings = Settings(data_dir=Path(config['directory']), mode='demo', erp_mode='mock')
        # This process owns a freshly created temporary SQLite DB, never an existing deployment.
        db = Database(settings.database_url, create_schema=True)
        service = ProcurementService(db, settings, NoERP())
        candidates = CandidateService(service)
        principal = Principal(**settings.auth_tokens['demo-buyer'])
        req = service.create_request(principal, RequestCreate(**case['request']))
        doc = candidates.upload(principal, req['id'], 'synthetic.txt', case['text'].encode())
        run = candidates.reserve(principal, req['id'], CandidateReserve(document_id=doc['id'],
            expected_version=req['version'], idempotency_key='candidate-eval-' + case['id']))
        candidates.enqueue(principal, run['id'], CandidateProcess(expected_revision=run['revision'], acknowledge_external_model=True))
        transport = CountedTransport()
        def factory():
            agent = CandidateAgent(config['base_url'], config['api_key'], config['model'],
                transport=transport, thinking_mode=config['thinking_mode'], max_connection_retries=0,
                max_model_calls=config['max_calls'], max_tool_calls=12,
                max_wall_seconds=config['wall_seconds'], max_reported_tokens=config['token_budget'], require_usage=True)
            original = agent.run
            def run_with_trace(invoke, evidence_ids, observer=None):
                def observe(event):
                    if event.get('type') == 'tool_started' and event.get('tool') in CandidateAgent.tool_schemas:
                        tools.append(event['tool'])
                    if observer:
                        observer(event)
                return original(invoke, evidence_ids, observer=observe)
            agent.run = run_with_trace
            return agent
        drain_once(candidates, factory=factory, limit=1)
        receipt = candidates.get(principal, run['id'])
        output = receipt.get('output')
        if receipt['status'] in {'NEEDS_REVIEW', 'NEEDS_CLARIFICATION'} and output:
            evidence = {k: [locator(ref, doc['sha256']) for ref in refs] for k, refs in output['evidence'].items()}
            report.update(status='observed', reason=None, fields=seal_fields(output['values'], evidence),
                clarifications={x['field']: x['reason'] for x in output['clarifications']}, usage=output.get('usage'))
        else:
            code = receipt.get('error_code')
            report.update(status='failed', reason=code if code in SAFE_ERRORS else 'CANDIDATE_FAILED')
        # No importing, confirming, approving or executing; inspect actual DB counts.
        with db.transaction() as session:
            report['side_effects'] = {name: session.scalar(select(func.count()).select_from(row))
                for name, row in [('quotes', QuoteRow), ('approvals', ApprovalRow), ('operations', OperationRow)]}
            report['side_effects']['erp_attempts'] = erp_attempts
    finally:
        report.update(provider_requests=calls, network_attempted=bool(calls) if mode == 'live' else False,
                      tool_path=tools[:12])
        if transport:
            transport.close()
        if db:
            db.engine.dispose()
            if db.fence_engine is not None:
                db.fence_engine.dispose()
    return report


def run(case, config):
    start = time.monotonic()
    try:
        result = baseline(case) if config['mode'] == 'baseline' else candidate(case, config)
    except ImportError:
        result = empty_record(case, config['mode'], 'DEPENDENCIES_UNAVAILABLE')
        result['status'] = 'failed'
    except Exception:
        # Raw exception values/keys/provider text are NEVER exported.
        result = empty_record(case, config['mode'], 'TRIAL_FAILED')
        result.update(status='failed', provider_requests=0 if config['mode'] == 'baseline' else None,
                      network_attempted=None if config['mode'] == 'live' else False)
    result['elapsed_seconds'] = round(time.monotonic() - start, 3)
    return result
