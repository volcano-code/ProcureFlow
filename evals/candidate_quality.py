"""Independent extraction-only Dev grader. No production parser/model imports.

Public observations contain value fingerprints and source-line fingerprints, not
provider prose. Gold-span agreement is NOT general semantic entailment evidence.
The separate runner never receives this module's gold loader or gold records.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

FIELDS = ('supplier_id', 'sku', 'quantity', 'uom', 'unit_price', 'tax_mode',
          'tax_rate', 'shipping_cost', 'discount', 'delivery_days', 'currency')
DECIMALS = {'quantity', 'unit_price', 'tax_rate', 'shipping_cost', 'discount'}
SHA = re.compile(r'^[0-9a-f]{64}$')


def require(ok, code):
    if not ok:
        raise ValueError(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def pairs(values):
    result = {}
    for key, value in values:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def load_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(500001)
    require(len(raw) <= 500000, 'INPUT_TOO_LARGE')
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: require(False, 'NONFINITE_JSON'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves previous attempts, including blocked reports.
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def load_inputs(directory):
    data = load_json(Path(directory) / 'inputs.json')
    if data.get('schema_version') == 2:
        require(data.get('suite') == 'candidate-extraction-dev20-v1', 'INVALID_SUITE')
        base = load_inputs(Path(directory).parent / 'candidate12')
        require(data.get('base_input_sha256') == fingerprint(base), 'BASE_INPUT_DRIFT')
        require(len(data.get('cases', [])) == 8, 'EXPECTED_EIGHT_ADDITIONS')
        data = {**data, 'schema_version': 1, 'cases': base['cases'] + data['cases']}
    expected = {'candidate-extraction-dev12-v1': 12, 'candidate-extraction-dev20-v1': 20}.get(data.get('suite'))
    require(expected is not None and data.get('schema_version') == 1 and data.get('split') == 'Dev'
            and data.get('synthetic_only') is True, 'INVALID_SUITE')
    cases = data.get('cases')
    require(isinstance(cases, list) and len(cases) == expected, 'INVALID_CASE_COUNT')
    require(len({x['id'] for x in cases}) == expected, 'DUPLICATE_CASE')
    for case in cases:
        require(re.fullmatch(r'cq-[0-9]{3}', case['id']) is not None, 'INVALID_CASE_ID')
        require(isinstance(case['text'], str) and 0 < len(case['text'].encode()) <= 12000,
                'INVALID_SOURCE')
        require(hashlib.sha256(case['text'].encode()).hexdigest() == case['sha256'], 'SOURCE_DRIFT')
        require(isinstance(case['family'], str) and isinstance(case['request'], dict), 'INVALID_CASE')
    return data


def is_known(field, value):
    return value is not None and not (field == 'tax_mode' and value == 'unknown')


def value_digest(field, value):
    require(field in FIELDS, 'INVALID_FIELD')
    if not is_known(field, value):
        return fingerprint(None)
    if field in DECIMALS:
        require(isinstance(value, str) and len(value) <= 40, 'DECIMAL_MUST_BE_STRING')
        try:
            number = Decimal(value)
            require(number.is_finite() and abs(number) <= Decimal('1000000000'), 'INVALID_DECIMAL')
            value = format(number.normalize(), 'f')
        except InvalidOperation:
            raise ValueError('INVALID_DECIMAL') from None
    elif field == 'delivery_days':
        require(type(value) is int and 1 <= value <= 365, 'INVALID_DELIVERY_DAYS')
    else:
        require(isinstance(value, str) and 0 < len(value) <= 160, 'INVALID_STRING')
        # Opaque identifiers remain case-sensitive; units/currency normalize.
        value = value.strip().upper() if field in {'uom', 'currency'} else value.strip()
    return fingerprint(value)


def seal_fields(values, evidence):
    """Do not export supplier/model strings, arbitrary keys or quotation prose."""
    result = {}
    for field in FIELDS:
        value = values.get(field, 'unknown' if field == 'tax_mode' else None)
        refs = evidence.get(field, [])
        result[field] = {'known': is_known(field, value), 'value_sha256': value_digest(field, value),
                         'references': refs}
    return result


SAFE_REASONS = frozenset({None, 'NOT_RUN', 'EXPLICIT_NETWORK_OPT_IN_REQUIRED',
    'MODEL_CONFIGURATION_REQUIRED', 'MODEL_CONFIGURATION_INVALID', 'OUTSIDE_EXPLICIT_CASE_LIMIT',
    'INSUFFICIENT_REMAINING_REQUEST_BUDGET', 'UNCERTAIN_PROVIDER_OUTCOME_NO_RETRY',
    'PROVIDER_FAILURE_OR_USAGE_UNKNOWN_NO_RETRY', 'WORKER_TIMEOUT_OR_INVALID_REPORT',
    'TRIAL_FAILED', 'DEPENDENCIES_UNAVAILABLE', 'MODEL_CALL_FAILED', 'MODEL_PROTOCOL_INVALID',
    'MODEL_TIMEOUT', 'MODEL_SCHEMA_INVALID', 'MODEL_USAGE_REQUIRED', 'MODEL_REFUSED',
    'BUDGET_EXCEEDED', 'MODEL_EMPTY_OUTPUT', 'MODEL_RESPONSE_LIMIT', 'MODEL_OUTPUT_INCOMPLETE',
    'EVIDENCE_NOT_FOUND', 'EVIDENCE_NOT_READ', 'EVIDENCE_REQUIRED', 'TOOL_POLICY_DENIED',
    'NO_RECOGNIZED_FIELDS', 'CANDIDATE_FAILED', 'CANDIDATE_OUTPUT_INVALID', 'PARSER_TIMEOUT'})


def validate_record(record, case, mode):
    keys = {'id', 'source_sha256', 'mode', 'status', 'reason', 'fields', 'clarifications',
        'side_effects', 'provider_requests', 'network_attempted', 'usage', 'cost', 'tool_path', 'elapsed_seconds'}
    require(isinstance(record, dict) and set(record) == keys, 'INVALID_RECORD_KEYS')
    require(record['id'] == case['id'] and record['source_sha256'] == case['sha256']
            and record['mode'] == mode, 'INVALID_RECORD_IDENTITY')
    require(record['status'] in {'observed', 'failed', 'blocked'} and record['reason'] in SAFE_REASONS,
            'INVALID_RECORD_STATUS')
    require(record['cost'] is None and (record['network_attempted'] is None
            or type(record['network_attempted']) is bool), 'INVALID_ACCOUNTING')
    require(record['provider_requests'] is None or (type(record['provider_requests']) is int
            and 0 <= record['provider_requests'] <= 4), 'INVALID_REQUEST_COUNT')
    require(type(record['elapsed_seconds']) in (int, float) and math.isfinite(record['elapsed_seconds'])
            and 0 <= record['elapsed_seconds'] <= 10000, 'INVALID_DURATION')
    require(isinstance(record['tool_path'], list) and len(record['tool_path']) <= 12
            and all(x in {'get_request', 'get_source_index', 'get_evidence'} for x in record['tool_path']),
            'INVALID_TOOL_PATH')
    usage = record['usage']
    require(usage is None or (isinstance(usage, dict)
            and set(usage) == {'prompt_tokens', 'completion_tokens', 'total_tokens'}
            and all(type(x) is int and 0 <= x <= 16000 for x in usage.values())
            and usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens']), 'INVALID_USAGE')
    fields = record['fields']
    require(isinstance(fields, dict) and set(fields).issubset(FIELDS), 'INVALID_FIELDS')
    if record['status'] == 'observed':
        require(set(fields) == set(FIELDS), 'INCOMPLETE_OBSERVED_FIELDS')
    for item in fields.values():
        require(isinstance(item, dict) and set(item) == {'known', 'value_sha256', 'references'}
                and type(item['known']) is bool and isinstance(item['value_sha256'], str)
                and SHA.fullmatch(item['value_sha256']), 'INVALID_FIELD_OBSERVATION')
        require(isinstance(item['references'], list) and len(item['references']) <= 4, 'INVALID_REFERENCES')
        for ref in item['references']:
            require(isinstance(ref, dict) and set(ref) == {'line', 'text_sha256', 'source_sha256'}
                and type(ref['line']) is int and 1 <= ref['line'] <= 2000
                and all(isinstance(ref[k], str) and SHA.fullmatch(ref[k]) for k in ('text_sha256', 'source_sha256')),
                'INVALID_LOCATOR')
    questions = record['clarifications']
    require(isinstance(questions, dict) and set(questions).issubset(FIELDS)
            and all(x in {'missing', 'ambiguous', 'conflict'} for x in questions.values()), 'INVALID_CLARIFICATIONS')
    effects = record['side_effects']
    require(effects is None or (isinstance(effects, dict)
            and set(effects) == {'quotes', 'approvals', 'operations', 'erp_attempts'}
            and all(type(x) is int and 0 <= x <= 1000000 for x in effects.values())), 'INVALID_SIDE_EFFECTS')
    if mode in {'fixture', 'baseline'}:
        require(record['network_attempted'] is False, 'OFFLINE_MODE_NETWORK_MISMATCH')
    if mode == 'baseline':
        require(record['provider_requests'] == 0 and usage is None, 'BASELINE_MODEL_CALL')
    return record


def load_gold(directory):
    gold = load_json(Path(directory) / 'gold.json')
    if gold.get('schema_version') == 2:
        base = load_json(Path(directory).parent / 'candidate12' / 'gold.json')
        require(gold.get('base_gold_sha256') == fingerprint(base), 'BASE_GOLD_DRIFT')
        require(len(gold.get('cases', [])) == 8, 'EXPECTED_EIGHT_GOLD_ADDITIONS')
        gold = {**gold, 'schema_version': 1, 'cases': base['cases'] + gold['cases']}
    return gold


def grade(directory, report):
    inputs = load_inputs(directory)
    gold = load_gold(directory)
    require(gold.get('suite') == inputs['suite'], 'GOLD_SUITE_MISMATCH')
    require(report.get('input_sha256') == fingerprint(inputs), 'REPORT_SOURCE_MISMATCH')
    require(report.get('schema_version') == 1 and report.get('attempt') == 1, 'INVALID_ATTEMPT')
    require(report.get('mode') in {'baseline', 'fixture', 'live'}, 'INVALID_RUN_MODE')
    require(SHA.fullmatch(report.get('implementation_sha256', '')) is not None, 'MISSING_IMPLEMENTATION')
    records = report.get('cases')
    require(isinstance(records, list) and len(records) == len(inputs['cases']), 'MISSING_CASES')
    ids = [c['id'] for c in inputs['cases']]
    require([x.get('id') for x in records] == ids, 'DUPLICATE_REORDERED_OR_UNKNOWN_CASE')
    require(len(gold.get('cases', [])) == len(ids) and [x['id'] for x in gold['cases']] == ids,
            'GOLD_CASE_MISMATCH')
    scores = []; known_total = known_correct = expected_unknown = preserved_unknown = 0
    cited = locatable = span_agreement = proposed = complete = passed = 0
    for case, answer, record in zip(inputs['cases'], gold['cases'], records):
        validate_record(record, case, report['mode'])
        require(record.get('source_sha256') == case['sha256'], 'CASE_SOURCE_MISMATCH')
        require(set(answer['values']) == set(FIELDS), 'INCOMPLETE_GOLD_VALUES')
        require(record.get('status') in {'observed', 'failed', 'blocked'}, 'INVALID_STATUS')
        require(record.get('mode') == report['mode'], 'MIXED_RUN_MODES')
        questions = record.get('clarifications', {})
        require(isinstance(questions, dict) and set(questions).issubset(FIELDS)
                and all(v in {'missing', 'ambiguous', 'conflict'} for v in questions.values()),
                'INVALID_CLARIFICATIONS')
        fields = record.get('fields', {})
        require(isinstance(fields, dict) and set(fields).issubset(FIELDS), 'INVALID_FIELDS')
        if record['status'] == 'observed':
            require(set(fields) == set(FIELDS), 'INCOMPLETE_OBSERVED_FIELDS')
            complete += 1
        correct = 0; support_ok = True
        lines = case['text'].splitlines()
        for field in FIELDS:
            expected = answer['values'][field]
            known = is_known(field, expected)
            known_total += int(known); expected_unknown += int(not known)
            observed = fields.get(field)
            match = False
            if observed is not None:
                require(set(observed) == {'known', 'value_sha256', 'references'}
                        and type(observed['known']) is bool and SHA.fullmatch(observed['value_sha256']),
                        'INVALID_FIELD_OBSERVATION')
                refs = observed['references']
                require(isinstance(refs, list) and len(refs) <= 4, 'INVALID_REFERENCES')
                match = observed['value_sha256'] == value_digest(field, expected) and observed['known'] == known
                if observed['known']:
                    proposed += 1
                    if not refs:
                        support_ok = False
                elif refs:
                    support_ok = False
                for ref in refs:
                    cited += 1
                    require(isinstance(ref, dict) and set(ref) == {'line', 'text_sha256', 'source_sha256'},
                            'INVALID_LOCATOR')
                    line = ref['line']
                    valid = (type(line) is int and 0 < line <= len(lines)
                        and ref['source_sha256'] == case['sha256']
                        and ref['text_sha256'] == hashlib.sha256(lines[line - 1].encode()).hexdigest())
                    locatable += int(valid)
                    supported = valid and match and line in answer['support_lines'].get(field, [])
                    span_agreement += int(supported)
                    support_ok = support_ok and supported
            correct += int(match)
            known_correct += int(known and match)
            preserved_unknown += int(not known and match)
        side_effects = record.get('side_effects')
        require(side_effects is None or (isinstance(side_effects, dict)
                and set(side_effects) == {'quotes', 'approvals', 'operations', 'erp_attempts'}
                and all(type(n) is int and n >= 0 for n in side_effects.values())), 'INVALID_SIDE_EFFECTS')
        safe = side_effects is not None and all(n == 0 for n in side_effects.values())
        exact = (record['status'] == 'observed' and correct == len(FIELDS)
                 and questions == answer['clarifications'] and support_ok and safe)
        passed += int(exact)
        scores.append({'id': case['id'], 'family': case['family'], 'status': record['status'],
                       'passed': exact, 'correct_fields': correct, 'fields': len(FIELDS),
                       'clarification_exact': questions == answer['clarifications'],
                       'citation_checks': support_ok, 'zero_side_effects_observed': safe})
    return {'schema_version': 1, 'suite': inputs['suite'], 'split': 'Dev', 'mode': report['mode'],
        'input_sha256': fingerprint(inputs), 'gold_sha256': fingerprint(gold),
        'observation_sha256': fingerprint(report), 'implementation_sha256': report['implementation_sha256'],
        'status': 'measured' if all(x['status'] != 'blocked' for x in records) else 'incomplete',
        'counts': {'tasks': len(ids), 'observed': complete, 'passed': passed, 'not_passed': len(ids)-passed},
        'metrics': {'known_fields_correct': known_correct, 'known_fields_total': known_total,
            'unknowns_preserved': preserved_unknown, 'expected_unknowns': expected_unknown,
            'proposed_fields': proposed, 'citations': cited, 'locatable_citations': locatable,
            'gold_span_agreements': span_agreement, 'semantic_support_rate': None},
        'model_quality_measured': report['mode'] == 'live' and all(x['status'] != 'blocked' for x in records)
            and any(x['network_attempted'] is True for x in records),
        'cost': None, 'reference_review': gold['review_status'], 'cases': scores,
        'limitations': ['Extraction-only, no policy selection or ERP acceptance.',
            'Public developer-authored Dev answers, not Holdout/generalization evidence.',
            'A valid location and a matching gold span are not general semantic entailment.',
            'Fixture scores NEVER certify real-model quality. No best-of retries.']}


def compare(directory, baseline, candidate):
    require(baseline.get('mode') == 'baseline' and candidate.get('mode') == 'live', 'NEED_BASELINE_AND_LIVE_NOT_FIXTURE')
    require(baseline.get('implementation_sha256') == candidate.get('implementation_sha256'), 'DIFFERENT_IMPLEMENTATIONS')
    left, right = grade(directory, baseline), grade(directory, candidate)
    paired = [{'id': a['id'], 'baseline_passed': a['passed'], 'candidate_passed': b['passed'],
        'candidate_status': b['status']} for a, b in zip(left['cases'], right['cases'])]
    complete = left['status'] == right['status'] == 'measured' and right['model_quality_measured']
    return {'schema_version': 1, 'status': 'measured' if complete else 'blocked_or_partial',
        'scope': 'Paired extraction Dev tasks, not general procurement performance.',
        'input_sha256': left['input_sha256'], 'baseline': left['counts'], 'candidate': right['counts'],
        'paired_net_improvement': (right['counts']['passed'] - left['counts']['passed']) if complete else None,
        'cases': paired, 'real_model_acceptance': complete, 'cost': None}
