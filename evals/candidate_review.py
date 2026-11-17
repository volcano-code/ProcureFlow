"""Explicit local human review, bound to public fingerprints; no automatic verdicts.

Packets contain proposed values and synthetic sources. Keep them outside Git and
CI artifacts. OS file permissions are not encryption or reviewer authentication.
"""
from __future__ import annotations
import os
from pathlib import Path
import json
from .candidate_quality import FIELDS, fingerprint, value_digest, require, validate_record, is_known

VERDICTS = {'supported', 'unsupported', 'unclear', 'appropriate_unknown', 'inappropriate_unknown'}


def write_private(path, value):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def validate_material(material, record):
    require(isinstance(material, dict) and set(material) == {'values', 'questions'}, 'INVALID_REVIEW_MATERIAL')
    values = material['values']
    require(isinstance(values, dict) and set(values) == set(FIELDS), 'INVALID_REVIEW_VALUES')
    for field in FIELDS:
        item = record['fields'][field]
        require(value_digest(field, values[field]) == item['value_sha256']
                and is_known(field, values[field]) == item['known'], 'REVIEW_VALUE_MISMATCH')
    questions = material['questions']
    require(isinstance(questions, list) and len(questions) <= 12, 'INVALID_REVIEW_QUESTIONS')
    require(all(isinstance(q, dict) and set(q) == {'field', 'reason', 'question'}
        and q['field'] in FIELDS and q['reason'] in {'missing', 'conflict', 'ambiguous'}
        and isinstance(q['question'], str) and 5 <= len(q['question']) <= 500 for q in questions), 'INVALID_REVIEW_QUESTION')
    require(len({q['field'] for q in questions}) == len(questions) and
        {q['field']: q['reason'] for q in questions} == record['clarifications'], 'REVIEW_QUESTION_MISMATCH')


def build_packet(inputs, report, materials):
    require(len(materials) == len(inputs['cases']) == len(report['cases']), 'REVIEW_CASES_MISSING')
    require(report['input_sha256'] == fingerprint(inputs), 'REVIEW_INPUT_MISMATCH')
    items = []
    for case, record, material in zip(inputs['cases'], report['cases'], materials):
        validate_record(record, case, report['mode'])
        if record['status'] != 'observed':
            require(material is None, 'UNOBSERVED_REVIEW_MATERIAL')
            continue
        validate_material(material, record)
        for field in FIELDS:
            item = {'case_id': case['id'], 'field': field, 'known': record['fields'][field]['known'],
                'proposed_value': material['values'][field], 'source_text': case['text'],
                'source_sha256': case['sha256'], 'references': record['fields'][field]['references'],
                'question': next((q for q in material['questions'] if q['field'] == field), None)}
            item['id'] = fingerprint(item)
            items.append(item)
    packet = {'schema_version': 1, 'scope': 'local human review of public synthetic Dev sources; not a model score',
        'input_sha256': fingerprint(inputs), 'observation_sha256': fingerprint(report),
        'tasks_total': len(inputs['cases']), 'tasks_observed': sum(r['status'] == 'observed' for r in report['cases']),
        'items': items}
    packet['packet_sha256'] = fingerprint(packet)
    return packet


def review_template(packet):
    return {'schema_version': 1, 'packet_sha256': packet['packet_sha256'],
        'reviewer_id': None, 'reviewer_role': None, 'attest_manual_review': False,
        'ratings': [{'id': x['id'], 'verdict': None, 'note': ''} for x in packet['items']]}


def summarize_review(inputs, report, packet, annotations):
    # Rebuild the immutable packet from the same report and selected private values.
    require(packet.get('packet_sha256') == fingerprint({k: v for k, v in packet.items() if k != 'packet_sha256'}), 'REVIEW_PACKET_DRIFT')
    material = []
    for record in report['cases']:
        rows = [x for x in packet['items'] if x['case_id'] == record['id']]
        if record['status'] != 'observed':
            require(not rows, 'UNOBSERVED_REVIEW_MATERIAL'); material.append(None); continue
        require(len(rows) == len(FIELDS) and {x['field'] for x in rows} == set(FIELDS), 'REVIEW_FIELDS_MISSING')
        material.append({'values': {x['field']: x['proposed_value'] for x in rows},
                         'questions': [x['question'] for x in rows if x['question'] is not None]})
    require(build_packet(inputs, report, material) == packet, 'REVIEW_SOURCE_OR_OBSERVATION_MISMATCH')
    require(set(annotations) == {'schema_version', 'packet_sha256', 'reviewer_id', 'reviewer_role', 'attest_manual_review', 'ratings'}
            and annotations['schema_version'] == 1 and annotations['packet_sha256'] == packet['packet_sha256'], 'INVALID_ANNOTATION_BINDING')
    ratings = annotations['ratings']
    require(isinstance(ratings, list) and [r.get('id') for r in ratings] == [i['id'] for i in packet['items']], 'REVIEW_RATINGS_MISSING_OR_REORDERED')
    require(type(annotations['attest_manual_review']) is bool, 'INVALID_REVIEW_ATTESTATION')
    counts = {v: 0 for v in VERDICTS}; pending = 0; known_count = 0
    for item, rating in zip(packet['items'], ratings):
        require(set(rating) == {'id', 'verdict', 'note'} and isinstance(rating['note'], str)
                and len(rating['note']) <= 1000, 'INVALID_REVIEW_RATING')
        known_count += int(item['known']); verdict = rating['verdict']
        if verdict is None:
            pending += 1; continue
        allowed = {'supported', 'unsupported', 'unclear'} if item['known'] else {'appropriate_unknown', 'inappropriate_unknown', 'unclear'}
        require(verdict in allowed, 'INCOMPATIBLE_REVIEW_VERDICT')
        require(bool(rating['note'].strip()), 'REVIEW_JUSTIFICATION_REQUIRED')
        counts[verdict] += 1
    reviewer = annotations['reviewer_id']; role = annotations['reviewer_role']
    require(reviewer is None or (isinstance(reviewer, str) and 1 <= len(reviewer.strip()) <= 160), 'INVALID_REVIEWER')
    require(role in {None, 'developer', 'external_reviewer'}, 'INVALID_REVIEWER_ROLE')
    complete = (len(ratings) > 0 and pending == 0 and packet['tasks_observed'] == packet['tasks_total']
                and bool(reviewer) and role is not None and annotations['attest_manual_review'])
    return {'schema_version': 1, 'status': 'review_record_complete' if complete else 'pending_or_partial',
        'observation_sha256': fingerprint(report), 'packet_sha256': packet['packet_sha256'],
        'annotations_sha256': fingerprint(annotations), 'reviewer_sha256': fingerprint(reviewer) if reviewer else None,
        'reviewer_role': role, 'reviewer_identity_verified': False,
        'counts': counts | {'pending': pending, 'items': len(ratings), 'proposed_known_fields': known_count},
        'semantic_support_rate': counts['supported']/known_count if complete and known_count else None,
        'scope': 'Self-attested human review record; not independent certification or generalization evidence.'}
