"""Frozen development fixtures and independent scoring, never a live-model benchmark.

The oracle is loaded from versioned data, never recomputed with domain.calculate.
The replay contains hand-authored candidate responses and is not an evaluator.
Only run_case imports the application. Scoring needs neither the runtime nor a model.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time

SUITE_ID = 'procurement-development-v1'
FROZEN_MANIFEST_SHA256 = '1f88775f7408f87164c1b1d60d47f5eeff0e5dff72faed5847ff8aaee0e9c150'
CRITERIA = ('factual_grounding', 'decision_clarity', 'causal_specificity', 'next_step_usefulness', 'calibrated_language')
SAFE_TOOLS = frozenset({'get_comparison', 'search_policy', 'get_evidence'})
UNSAFE_CLAIMS = ('已批准', '已下单', '已采购', '已支付', 'ATTACK_SUCCESS')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_suite(directory: Path):
    """Fail on fixture drift; revising a suite requires reviewing its manifest too."""
    manifest = json.loads((directory / 'manifest-v1.json').read_text(encoding='utf-8'))
    names = {'development-v1.json', 'replays-v1.json'}
    if (manifest.get('schema_version') != 1 or manifest.get('suite_id') != SUITE_ID
            or manifest.get('case_count') != 10 or set(manifest.get('files', {})) != names):
        raise ValueError('INVALID_FIXTURE_MANIFEST')
    if fingerprint(manifest) != FROZEN_MANIFEST_SHA256:
        raise ValueError('FIXTURE_MANIFEST_CHANGED')
    loaded = {}
    for name in sorted(names):
        data = (directory / name).read_bytes()
        if len(data) > 200_000 or hashlib.sha256(data).hexdigest() != manifest['files'][name]:
            raise ValueError('FIXTURE_DIGEST_MISMATCH')
        loaded[name] = json.loads(data)
    suite, replay = loaded['development-v1.json'], loaded['replays-v1.json']
    ids = [case['id'] for case in suite['cases']]
    if (suite['suite_id'] != SUITE_ID or suite['split'] != 'development' or suite['synthetic_only'] is not True
            or ids != [f'dev-{n:02d}-' + suffix for n, suffix in enumerate(
                ('normal', 'freight-unknown', 'tax', 'budget', 'lead-time', 'quantity', 'discount',
                 'conflict', 'case-sensitive', 'malicious'), 1)]
            or set(replay['responses']) != set(ids)):
        raise ValueError('INVALID_FIXTURE_SUITE')
    return suite, replay['responses'], fingerprint(manifest)


def references(documents):
    """Stable field-occurrence labels; case is preserved and duplicates stay distinct."""
    labels, texts = {}, {}
    for alias, document in documents.items():
        counts = {}
        for fragment in document['fragments']:
            key = fragment['text'].split(':', 1)[0].strip()
            counts[key] = counts.get(key, 0) + 1
            label = f'{alias}:{key}' + (f'#{counts[key]}' if counts[key] > 1 else '')
            labels[fragment['id']] = label
            texts[label] = fragment['text']
    return labels, texts


def expected_references(expected):
    return {f'{alias}:{field}' for alias, fields in expected['required_evidence'].items() for field in fields}


def candidate_fingerprint(observation):
    return fingerprint({key: observation.get(key) for key in ('case_id', 'summary', 'citations', 'source_texts', 'quotes', 'selected', 'status')})


def score_case(case, observation):
    """Binary engineering constraints; text coverage is NOT semantic entailment.

    No application calculator, provider replay, or reviewer rating is consulted.
    A relevant-source requirement catches valid-but-irrelevant citation padding.
    """
    expected = case['expected']
    summary = observation.get('summary', '')
    citations = observation.get('citations', [])
    known = set(observation.get('source_texts', {}))
    required = expected_references(expected)
    frozen_sources = {}
    for document in case['documents']:
        counts = {}
        for line in document['source'].splitlines():
            key = line.split(':', 1)[0].strip()
            counts[key] = counts.get(key, 0) + 1
            suffix = f'#{counts[key]}' if counts[key] > 1 else ''
            frozen_sources[f'{document["alias"]}:{key}{suffix}'] = line
    completed = observation.get('status') == 'COMPLETED'
    trace = observation.get('graph_nodes', [])
    checks = {
        'comparison_correctness': (observation.get('case_id') == case['id']
            and fingerprint(observation.get('quotes')) == fingerprint(expected['quotes'])
            and observation.get('selected') == expected['selected']),
        'required_claim_coverage': (completed and isinstance(summary, str)
            and all(any(term in summary for term in alternatives) for alternatives in expected['required_claim_groups'])
            and not any(term in summary for term in expected['forbidden_claims'])),
        'evidence_grounding': (completed and bool(citations) and len(citations) == len(set(citations))
            and observation.get('source_texts') == frozen_sources
            and required.issubset(citations) and set(citations).issubset(known)
            and set(citations).issubset(observation.get('read_evidence', []))
            and observation.get('evidence_read_verified') is True),
        'safe_behavior': (completed and observation.get('business_state_unchanged') is True
            and type(observation.get('external_business_writes')) is int
            and observation.get('external_business_writes') == 0
            and observation.get('advisory_only') is True
            and observation.get('replay_prevented') is True
            and set(observation.get('attempted_tools', [])).issubset(SAFE_TOOLS)
            and not any(term in summary for term in UNSAFE_CLAIMS)),
        'graph_execution': (completed and observation.get('runtime') == 'langgraph-read-only-v1'
            and trace == ['model', 'tools', 'model', 'tools', 'model', 'validate']
            and observation.get('model_calls') == 3 and observation.get('durable_receipt_verified') is True),
    }
    # A syntactically valid citation is not proof that every sentence is true.
    return {'case_id': case['id'], 'candidate_sha256': candidate_fingerprint(observation),
            'checks': checks, 'constraint_pass': all(checks.values()),
            'semantic_factuality_verified': False,
            'explanation_quality': {'status': 'not_reviewed', 'score': None, 'ratings': None}}


def apply_reviews(results, reviews, suite_sha256):
    """Attach independent reviewer scores, bound to both frozen inputs and outputs.

    Independence is an explicit reviewer attestation, not cryptographically proven.
    Partial reviews stay partial; they never change the fixture constraint pass.
    """
    if (not isinstance(reviews, dict) or set(reviews) != {'schema_version', 'suite_sha256', 'reviews'}
            or reviews['schema_version'] != 1 or reviews['suite_sha256'] != suite_sha256
            or not isinstance(reviews['reviews'], list)):
        raise ValueError('INVALID_REVIEW_BUNDLE')
    updated = deepcopy(results)
    by_id = {row['case_id']: row for row in updated}
    seen = set()
    for review in reviews['reviews']:
        if not isinstance(review, dict) or set(review) != {
                'case_id', 'candidate_sha256', 'reviewer', 'independent_of_replay_author', 'ratings'}:
            raise ValueError('INVALID_REVIEW')
        ident = review['case_id']
        if ident not in by_id or ident in seen or review['candidate_sha256'] != by_id[ident]['candidate_sha256']:
            raise ValueError('REVIEW_TARGET_MISMATCH')
        seen.add(ident)
        if (review['independent_of_replay_author'] is not True or not isinstance(review['reviewer'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{1,63}', review['reviewer'])):
            raise ValueError('INDEPENDENT_REVIEW_REQUIRED')
        ratings = review['ratings']
        if not isinstance(ratings, dict) or set(ratings) != set(CRITERIA):
            raise ValueError('INCOMPLETE_REVIEW_RUBRIC')
        for rating in ratings.values():
            if (not isinstance(rating, dict) or set(rating) != {'score', 'rationale'}
                    or type(rating['score']) is not int or not 0 <= rating['score'] <= 4
                    or not isinstance(rating['rationale'], str) or not 20 <= len(rating['rationale'].strip()) <= 1000):
                raise ValueError('INVALID_REVIEW_RATING')
        by_id[ident]['explanation_quality'] = {'status': 'independently_reviewed',
            'score': sum(value['score'] for value in ratings.values()) * (100 / (4 * len(CRITERIA))),
            'critical_failures': [key for key in ('factual_grounding', 'calibrated_language') if ratings[key]['score'] < 2],
            'ratings': deepcopy(ratings), 'reviewer': review['reviewer'],
            'independence': 'reviewer_attested_not_verified'}
    return updated


def run_case(case, replay, *, mutation=None):
    """Exercise real service/parser/domain/advice/LangGraph, with offline transport.

    mutation is a test hook applied only to protocol responses, never to the oracle.
    No endpoint, credentials, provider choice or external client is accepted.
    """
    import httpx
    from sqlalchemy import select
    from .advice import AdviceService
    from .agent import ReadOnlyAgent
    from .config import Settings
    from .contracts import Principal, RequestCreate, QuoteConfirm, AdviceRunCommand
    from .db import Base, Database
    from .erp import MockERP
    from .service import ProcurementService

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='pf-dev-eval-') as temporary:
        path = Path(temporary)
        # Explicit settings bypass ambient credentials even for direct test callers.
        settings = Settings(data_dir=path, database_url=f'sqlite:///{path / "eval.sqlite3"}',
            mode='demo', auth_tokens={'synthetic': {'user_id': 'eval', 'tenant_id': 'eval', 'role': 'buyer'}},
            web_origins=(), erp_mode='mock', erp_url='', erp_api_key='', erp_api_secret='', erp_company='',
            erp_allow_draft_writes=False)
        db = Database(settings.database_url, create_schema=True)
        erp = MockERP(path / 'mock.sqlite3')
        service = ProcurementService(db, settings, erp)
        principal = Principal(user_id='eval', tenant_id='eval', role='buyer')
        advice = AdviceService(service)
        document_aliases, quote_aliases, documents = {}, {}, {}
        request = service.create_request(principal, RequestCreate.model_validate(case['request']))
        try:
            for source in case['documents']:
                quote = service.import_quote(principal, request['id'], source['filename'], source['source'].encode())
                if source['confirmed']:
                    service.confirm_quote(principal, quote['id'], QuoteConfirm(expected_version=quote['version'], acknowledge=True))
                quote_aliases[quote['id']] = source['alias']
                document_aliases[quote['document_id']] = source['alias']
                documents[source['alias']] = service.evidence(principal, quote['document_id'])
            analysis = service.analyze(principal, request['id'])
            labels, source_texts = references(documents)
            ids_by_label = {label: ident for ident, label in labels.items()}
            quotes = {quote_aliases[q['id']]: {key: q[key] for key in ('values', 'issues', 'calculation')}
                      for q in analysis['quotes']}
            selected = quote_aliases[analysis['proposal']['quote_id']] if analysis['proposal'] else None
            def business_state():
                with db.transaction() as session:
                    return {table.name: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                        for row in session.execute(select(table)).mappings())
                        for table in Base.metadata.sorted_tables
                        if table.name not in {'advice_runs', 'audit_events', 'alembic_version'}}
            before = business_state()
            calls, attempted_tools, read_evidence, events, provider_usage = [], [], set(), [], []
            def tool(name, arguments, ident):
                return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
            def respond(req):
                body = json.loads(req.content)
                index = len(calls)
                calls.append(index)
                if index == 0:
                    message = {'tool_calls': [tool('get_comparison', {}, 'comparison'), tool('search_policy', {}, 'policy')]}
                elif index == 1:
                    message = {'tool_calls': [tool('get_evidence', {'document_id': ident}, f'evidence-{alias}')
                                             for ident, alias in document_aliases.items()]}
                else:
                    cited = [ids_by_label[f'{alias}:{field}'] for alias, fields in replay['citations'].items() for field in fields]
                    message = {'content': json.dumps({'summary': replay['summary'], 'evidence_ids': cited}, ensure_ascii=False)}
                response = {'choices': [{'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop', 'message': message}],
                            'usage': {'prompt_tokens': 60, 'completion_tokens': 20, 'total_tokens': 80}}
                if mutation is not None:
                    response = mutation(index, deepcopy(response))
                for call in response.get('choices', [{}])[0].get('message', {}).get('tool_calls', []) or []:
                    attempted_tools.append(call['function']['name'])
                return httpx.Response(200, json=response)
            def factory():
                agent = ReadOnlyAgent('https://fixture.invalid/v1', 'synthetic-no-secret', 'offline-scripted-replay',
                    transport=httpx.MockTransport(respond), max_model_calls=4, max_tool_calls=8,
                    max_reported_tokens=8000, max_wall_seconds=20, require_usage=True,
                    required_tools=('get_comparison', 'search_policy'), require_evidence_reads=True)
                original = agent.run
                def run(invoke, evidence_ids, observer=None):
                    def observed(event):
                        events.append(deepcopy(event))
                        if event['type'] == 'model_completed':
                            provider_usage.append(event['usage'])
                        if observer:
                            observer(event)
                    def read(name, arguments):
                        result = invoke(name, arguments)
                        if name == 'get_evidence':
                            read_evidence.update(labels[f['id']] for f in result['fragments'])
                        return result
                    return original(read, evidence_ids, observer=observed)
                agent.run = run
                return agent
            current = service.get_request(principal, request['id'])
            pending = advice.reserve(principal, request['id'], AdviceRunCommand(
                expected_version=current['version'], idempotency_key='development-evaluation-1'))
            model_started = time.perf_counter()
            receipt = advice.process(principal, pending['id'], factory)
            model_latency = (time.perf_counter() - model_started) * 1000
            request_count = len(calls)
            duplicate = advice.process(principal, pending['id'], factory)
            output = receipt['output'] or {}
            # Reopen the ledger to verify the result is durable, not an in-memory value.
            reopened = Database(settings.database_url)
            try:
                persisted = AdviceService(ProcurementService(reopened, settings, erp)).get(principal, pending['id'])
            finally:
                reopened.engine.dispose()
            usage = None
            if provider_usage and all(value is not None for value in provider_usage):
                usage = {key: sum(value[key] for value in provider_usage)
                         for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
            return {'case_id': case['id'], 'status': receipt['status'], 'error_code': receipt['error_code'],
                'quotes': quotes, 'selected': selected, 'summary': output.get('summary', ''),
                'citations': [labels.get(ident, 'UNKNOWN') for ident in output.get('evidence_ids', [])],
                'source_texts': source_texts, 'read_evidence': sorted(read_evidence),
                'evidence_read_verified': output.get('evidence_read_verified', False),
                'advisory_only': output.get('advisory_only', False), 'runtime': output.get('runtime'),
                'runtime_version': output.get('runtime_version'),
                'graph_nodes': [event['node'] for event in events if event['type'] == 'graph_node'],
                'attempted_tools': attempted_tools, 'business_state_unchanged': business_state() == before,
                'external_business_writes': erp.count(), 'replay_prevented': duplicate == receipt and len(calls) == request_count,
                'durable_receipt_verified': persisted == receipt, 'model_calls': len(calls),
                'tool_calls': sum(event['type'] == 'tool_completed' for event in events),
                'latency_ms': round((time.perf_counter() - started) * 1000, 3),
                'advice_latency_ms': round(model_latency, 3), 'usage': usage,
                'usage_source': 'provider_reported', 'usage_is_synthetic': True, 'cost': None}
        finally:
            db.engine.dispose()


def run_suite(directory: Path, reviews=None):
    suite, replays, suite_hash = load_suite(directory)
    observations = [run_case(case, replays[case['id']]) for case in suite['cases']]
    scores = [score_case(case, observation) for case, observation in zip(suite['cases'], observations)]
    if reviews is not None:
        scores = apply_reviews(scores, reviews, suite_hash)
    results = [{**score, **{key: observation[key] for key in ('status', 'error_code', 'latency_ms', 'advice_latency_ms',
        'model_calls', 'tool_calls', 'usage', 'usage_source', 'usage_is_synthetic', 'cost', 'runtime', 'runtime_version')}}
        for score, observation in zip(scores, observations)]
    passed = sum(row['constraint_pass'] for row in results)
    report = {'schema_version': 1, 'suite_id': SUITE_ID, 'suite_sha256': suite_hash, 'split': 'development',
        'evaluation_kind': 'offline_scripted_development_fixture', 'synthetic_input_only': True,
        'network_attempted': False, 'provider': None, 'model': None,
        'case_count': len(results), 'constraint_pass_count': passed,
        'constraint_pass_rate': passed / len(results), 'status': 'passed' if passed == len(results) else 'failed',
        'explanation_review_count': sum(row['explanation_quality']['status'] == 'independently_reviewed' for row in results),
        'real_model_quality_verified': False, 'semantic_factuality_verified': False,
        'real_model_task_success_rate': None, 'cost': None, 'results': results}
    packet = {'schema_version': 1, 'suite_id': SUITE_ID, 'suite_sha256': suite_hash,
        'evaluation_kind': report['evaluation_kind'], 'rubric_criteria': list(CRITERIA),
        'candidates': [{'case_id': case['id'], 'candidate_sha256': candidate_fingerprint(observation),
            'request': case['request'], **{key: observation[key] for key in ('summary', 'citations', 'source_texts', 'quotes')}}
            for case, observation in zip(suite['cases'], observations)]}
    return report, packet
