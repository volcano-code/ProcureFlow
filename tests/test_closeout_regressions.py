"""Adversarial evidence regressions; synthetic records are never runtime acceptance."""
import importlib.util
import json
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from check_candidate_procurement import check
from closeout import readiness

spec = importlib.util.spec_from_file_location('old_closeout_helpers', ROOT / 'tests/test_closeout.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


@pytest.mark.parametrize('fault', ['unrelated_cases', 'duplicate_names', 'summary_failure'])
def test_readiness_rejects_count_only_junit(tmp_path, fault):
    paths = helpers.dirs(tmp_path)
    names = [f'not_a_joint_test_{i}' for i in range(34)]
    if fault == 'duplicate_names':
        names = ['same_test'] * 34
    attrs = ' tests="34" failures="1" errors="0" skipped="0"' if fault == 'summary_failure' else ''
    (paths[0] / 'tests.xml').write_text('<testsuite' + attrs + '>' + ''.join(
        f'<testcase classname="unrelated" name="{name}"/>' for name in names) + '</testsuite>')
    assert readiness('a' * 40, paths)['status'] == 'blocked'


@pytest.mark.parametrize('field', ['operation_id', 'candidate_run_id'])
@pytest.mark.parametrize('value', [None, '', 17, False])
def test_joint_record_requires_nonempty_string_identity(tmp_path, field, value):
    run, audit = helpers.fixtures.records()
    run['operations'][2][field] = value
    helpers.fixtures.write(tmp_path, run, audit)
    with pytest.raises(ValueError):
        check(tmp_path)
