"""Comparable A/B/C summaries. A completed measurement is not a release approval."""
from __future__ import annotations
from math import ceil
from .candidate_quality import grade, require, fingerprint, load_inputs, load_gold, FIELDS, is_known, value_digest


def validate_gold(directory):
    inputs, gold = load_inputs(directory), load_gold(directory)
    require(gold['suite'] == inputs['suite'] and len(gold['cases']) == len(inputs['cases']), 'GOLD_SUITE_MISMATCH')
    for case, answer in zip(inputs['cases'], gold['cases']):
        require(answer['id'] == case['id'] and set(answer['values']) == set(FIELDS), 'GOLD_ID_OR_FIELDS')
        known = {k for k, v in answer['values'].items() if is_known(k, v)}
        require(set(answer['support_lines']) == known, 'GOLD_KNOWN_SUPPORT_MISMATCH')
        require(set(answer['clarifications']) == set(FIELDS)-known
            and all(v in {'missing', 'ambiguous', 'conflict'} for v in answer['clarifications'].values()), 'GOLD_UNKNOWN_MISMATCH')
        for field, value in answer['values'].items():
            value_digest(field, value)
        for refs in answer['support_lines'].values():
            require(isinstance(refs, list) and 1 <= len(refs) <= 4 and len(set(refs)) == len(refs)
                and all(type(i) is int and 1 <= i <= len(case['text'].splitlines()) for i in refs), 'INVALID_GOLD_SPAN')
    return {'status': 'passed', 'scope': 'reference structure only, not semantic or external review',
        'tasks': len(inputs['cases']), 'input_sha256': fingerprint(inputs), 'gold_sha256': fingerprint(gold)}


def accounting(report):
    rows = report['cases']; durations = sorted(x['elapsed_seconds'] for x in rows if x['status'] != 'blocked')
    usages = [r['usage'] for r in rows if r['usage'] is not None]
    return {'attempts': len(rows), 'observed': sum(x['status'] == 'observed' for x in rows),
        'blocked': sum(x['status'] == 'blocked' for x in rows),
        'known_provider_requests': sum(r['provider_requests'] or 0 for r in rows),
        'unknown_request_counts': sum(r['provider_requests'] is None for r in rows),
        'reported_tokens_known': sum(x['total_tokens'] for x in usages),
        'cases_with_usage': len(usages), 'usage_source': 'provider_reported_not_billing', 'cost': None,
        'elapsed_p50_seconds': durations[(len(durations)-1)//2] if durations else None,
        'elapsed_p95_seconds': durations[ceil(.95*len(durations))-1] if durations else None,
        'latency_scope': 'non-blocked per-case runtime including source prep; not provider-only latency'}


def compare_methods(directory, a, b, c):
    require(a.get('mode') == 'baseline' and a.get('strategy') == 'rules', 'EXPECTED_RULE_BASELINE')
    require(b.get('mode') == c.get('mode') == 'live', 'LIVE_METHODS_REQUIRED_NOT_FIXTURE')
    require(b.get('strategy') == 'single_shot' and c.get('strategy') == 'agent', 'EXPECTED_SINGLE_SHOT_AND_AGENT')
    for key in ('input_sha256', 'implementation_sha256'):
        require(a.get(key) == b.get(key) == c.get(key), 'COMPARISON_BINDING_MISMATCH')
    for key in ('model_sha256', 'endpoint_sha256', 'thinking_mode', 'comparison_protocol', 'context_limit_chars'):
        require(key in b and key in c and b[key] == c[key], 'MODEL_OR_CONTEXT_MISMATCH')
    require(b['comparison_protocol'] == 'same-candidate-contract-source-scope-v1' and b['context_limit_chars'] == 60000,
            'UNSUPPORTED_COMPARISON_PROTOCOL')
    for report, calls in ((b, 1), (c, 4)):
        budgets = report.get('budgets', {})
        for key, expected in {'per_case_calls': calls, 'per_case_tools': 12, 'per_case_wall_seconds': 45,
            'per_case_reported_tokens': 16000, 'output_tokens_per_request': 1600, 'hard_currency_budget': False}.items():
            require(type(budgets.get(key)) is type(expected) and budgets.get(key) == expected, 'COMPARISON_BUDGET_MISMATCH')
        require(all(r['provider_requests'] is None or r['provider_requests'] <= calls for r in report['cases']), 'STRATEGY_REQUEST_LIMIT')
    scores = [grade(directory, r) for r in (a, b, c)]
    complete = (all(x['status'] == 'measured' for x in scores)
        and all(x['model_quality_measured'] for x in scores[1:])
        and b['model_sha256'] is not None and b['endpoint_sha256'] is not None)
    pairs = [{'id': ra['id'], 'A': ra['passed'], 'B': rb['passed'], 'C': rc['passed'],
        'B_status': rb['status'], 'C_status': rc['status']} for ra, rb, rc in zip(*(x['cases'] for x in scores))]
    return {'schema_version': 1, 'status': 'measured' if complete else 'blocked_or_partial',
        'suite': scores[0]['suite'], 'input_sha256': scores[0]['input_sha256'],
        'observation_sha256': {k: fingerprint(r) for k, r in zip(('A', 'B', 'C'), (a, b, c))},
        'counts': {k: s['counts'] for k, s in zip(('A', 'B', 'C'), scores)},
        'accounting': {k: accounting(r) for k, r in zip(('A', 'B', 'C'), (a, b, c))},
        'C_minus_B_tasks': scores[2]['counts']['passed']-scores[1]['counts']['passed'] if complete else None,
        'B_minus_A_tasks': scores[1]['counts']['passed']-scores[0]['counts']['passed'] if complete else None,
        'paired': pairs, 'real_model_comparison_complete': complete,
        'limits': ['Public synthetic Dev suite, not unseen Holdout or procurement success rate.',
            'Same source scope, validator, model and per-case time/token ceilings; B one call vs C up to four.',
            'B fixed full retrieval vs C adaptive retrieval; prompts differ in routing, not a pure prompt ablation.',
            'Reported usage is not billing. No best-of selection. Human semantic review remains separate.']}
