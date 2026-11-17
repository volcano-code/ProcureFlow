"""Published pilot browser evidence never includes secret-bearing diagnostics."""
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[3]


def test_pilot_junit_preserves_outcomes_without_failure_or_capture_contents(tmp_path):
    spec=importlib.util.spec_from_file_location('pilot_native_gate',ROOT/'scripts/verify_next.py')
    gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
    private=tmp_path/'private.xml';public=tmp_path/'public.xml'
    private.write_text('''<testsuites><testsuite tests="3" failures="1" errors="1" skipped="0" time="1.5">
      <testcase classname="pilot" name="passed" time=".5"><system-out>pfi_synthetic_secret</system-out></testcase>
      <testcase classname="pilot" name="failed" time=".5"><failure message="fill(pfi_synthetic_secret)">pfs_synthetic_secret</failure></testcase>
      <testcase classname="pilot" name="error" time=".5"><error message="pfs_synthetic_secret">pfi_synthetic_secret</error><system-err>pfi_synthetic_secret</system-err></testcase>
      <system-out>pfs_synthetic_secret</system-out></testsuite></testsuites>''')
    gate.write_safe_pilot_junit(private,public)
    content=public.read_text()
    assert 'pfi_' not in content and 'pfs_' not in content and 'system-out' not in content and 'system-err' not in content
    suite=ET.parse(public).getroot().find('testsuite')
    assert suite.attrib == {'tests':'3','failures':'1','errors':'1','skipped':'0','time':'1.5'}
    assert [item.attrib['name'] for item in suite.findall('testcase')] == ['passed','failed','error']
    assert len(suite.findall('.//failure')) == 1 and len(suite.findall('.//error')) == 1
