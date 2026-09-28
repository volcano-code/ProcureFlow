import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("native_gate", ROOT / "scripts/verify_compose_web.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def make_report(path, names=None, flag=None):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="7", failures="0", errors="0", skipped="0")
    for name in sorted(gate.EXPECTED if names is None else names):
        case = ET.SubElement(suite, "testcase", name=name)
        if flag:
            ET.SubElement(case, flag)
    ET.ElementTree(root).write(path)


def test_native_gate_requires_all_named_cases(tmp_path):
    path = tmp_path / "result.xml"
    make_report(path)
    assert gate.report_passed(path)


@pytest.mark.parametrize("kind", ["missing", "broken", "few", "duplicates", "skipped", "failure", "error", "aggregate_error"])
def test_native_gate_cannot_report_false_success(tmp_path, kind):
    path = tmp_path / "result.xml"
    if kind == "missing":
        pass
    elif kind == "broken":
        path.write_text("<broken>")
    elif kind == "few":
        make_report(path, names=list(gate.EXPECTED)[:-1])
    elif kind == "duplicates":
        make_report(path, names=[next(iter(gate.EXPECTED))] * 7)
    elif kind == "aggregate_error":
        make_report(path)
        tree = ET.parse(path)
        tree.getroot().find("testsuite").set("errors", "1")
        tree.write(path)
    else:
        make_report(path, flag=kind)
    assert not gate.report_passed(path)


def test_native_gate_blocks_without_network_or_mutations(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.httpx, "Client", lambda **_: pytest.fail("No network"))
    assert gate.run("http://127.0.0.1:3000", tmp_path, False) == 2


@pytest.mark.parametrize("url", ["https://public.example", "http://127.0.0.1:3000/path", "http://user:pass@127.0.0.1"])
def test_native_gate_refuses_arbitrary_targets(tmp_path, monkeypatch, url):
    monkeypatch.setattr(gate.httpx, "Client", lambda **_: pytest.fail("No network"))
    assert gate.run(url, tmp_path, True) == 2
