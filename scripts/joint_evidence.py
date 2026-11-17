"""Bounded joint-gate evidence checks; consistency, not third-party attestation."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'scripts/joint_tests.json'
MAX_XML = 2 * 1024 * 1024


def require(ok, code):
    if not ok:
        raise ValueError(code)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_KEY')
        result[key] = value
    return result


def read_json(path, limit=100000):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'OVERSIZED_RECORD')
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, 'NONFINITE_JSON'))


def required_cases():
    record = read_json(MANIFEST)
    require(record.get('schema_version') == 1 and record.get('test_file') ==
            'tests/test_candidate_procurement.py', 'TEST_MANIFEST_INVALID')
    require(hashlib.sha256((ROOT / record['test_file']).read_bytes()).hexdigest() ==
            record.get('test_source_sha256'), 'TEST_SOURCE_CHANGED')
    cases = record.get('cases')
    require(isinstance(cases, list) and len(cases) >= 34 and
            all(isinstance(case, str) and case.startswith('test_') for case in cases) and
            len(set(cases)) == len(cases), 'TEST_MANIFEST_INVALID')
    return record, set(cases)


def check_junit(path):
    manifest, expected = required_cases()
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_XML + 1)
    require(len(raw) <= MAX_XML and b'<!DOCTYPE' not in raw.upper() and
            b'<!ENTITY' not in raw.upper(), 'UNSAFE_JUNIT')
    root = ET.fromstring(raw)
    require(root.tag in {'testsuites', 'testsuite'}, 'JUNIT_ROOT')
    suites = list(root.iter('testsuite'))
    require(bool(suites) and all(not suite.findall('testsuite') for suite in suites), 'JUNIT_LAYOUT')
    require(not any(list(root.iter(tag)) for tag in ('failure', 'error', 'skipped')), 'JOINT_TEST_FAILURE')
    cases = list(root.iter('testcase'))
    actual = []
    for suite in suites:
        own = suite.findall('testcase')
        for key, value in {'tests': len(own), 'failures': 0, 'errors': 0, 'skipped': 0}.items():
            require(suite.get(key) == str(value), 'JUNIT_SUMMARY_MISMATCH')
    for key, value in {'tests': len(cases), 'failures': 0, 'errors': 0, 'skipped': 0}.items():
        if root.tag == 'testsuites' and key in root.attrib:
            require(root.get(key) == str(value), 'JUNIT_ROOT_SUMMARY_MISMATCH')
    for case in cases:
        require(case.get('classname') == 'tests.test_candidate_procurement', 'WRONG_TEST_MODULE')
        actual.append(case.get('name'))
    require(len(actual) == len(expected) and len(set(actual)) == len(actual) and
            set(actual) == expected, 'JOINT_TEST_NAMES_MISMATCH')
    return {'tests': len(cases), 'junit_sha256': hashlib.sha256(raw).hexdigest(),
            'test_source_sha256': manifest['test_source_sha256']}


def check_execution(record, commit, tree):
    require(isinstance(record, dict) and record.get('schema_version') == 1, 'EXECUTION_INVALID')
    require(record.get('repository') == 'volcano-code/ProcureFlow' and
            record.get('source_commit') == commit and record.get('source_tree') == tree,
            'EXECUTION_SOURCE_MISMATCH')
    for field in ('run_id', 'run_attempt'):
        value = record.get(field)
        require(isinstance(value, str) and re.fullmatch('[1-9][0-9]{0,19}', value), 'EXECUTION_ID_INVALID')
    replica = record.get('replica')
    require(type(replica) is int and replica in (1, 2, 3), 'EXECUTION_REPLICA_INVALID')
    require(record.get('project') == f"pf-joint-{record['run_id']}-{record['run_attempt']}-{replica}",
            'EXECUTION_PROJECT_MISMATCH')
    return (record['run_id'], record['run_attempt'], replica)


def record_execution(directory):
    require(os.environ.get('GITHUB_ACTIONS') == 'true', 'CI_EXECUTION_REQUIRED')
    identity = subprocess.check_output(['git', 'rev-parse', 'HEAD', 'HEAD^{tree}'], cwd=ROOT,
                                       text=True).splitlines()
    require(len(identity) == 2 and all(re.fullmatch('[0-9a-f]{40}', value) for value in identity),
            'SOURCE_IDENTITY_INVALID')
    raw_replica = os.environ.get('PF_JOINT_REPLICA', '')
    require(raw_replica in {'1', '2', '3'}, 'EXECUTION_REPLICA_INVALID')
    record = {'schema_version': 1, 'source_commit': identity[0], 'source_tree': identity[1],
              'repository': os.environ.get('GITHUB_REPOSITORY'),
              'run_id': os.environ.get('GITHUB_RUN_ID'),
              'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
              'replica': int(raw_replica), 'project': os.environ.get('COMPOSE_PROJECT_NAME')}
    check_execution(record, *identity)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'execution.json').open('x', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        record_execution(args.directory)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print('{"status":"failed","reason":"EXECUTION_CONTEXT_INVALID"}')
        return 1
    print('{"status":"recorded","scope":"CI metadata, not a cryptographic attestation"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
