"""Seed ordering and safety tests; real master-data acceptance runs in ERP CI."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[3]


def runtime():
    spec = importlib.util.spec_from_file_location('lab_runtime_seed_test',
        ROOT / 'integrations/erpnext/sandbox/lab_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('existing', [False, True])
def test_seed_bootstraps_reference_fixtures_before_company_without_bypassing_links(monkeypatch, existing):
    from contextlib import nullcontext
    events = []
    class StopAfterCompany(Exception):
        pass
    def doc(values):
        if values['doctype'] == 'Company':
            assert events == ['fixtures']
            assert not any(k.startswith('ignore_') for k in values)
            raise StopAfterCompany()
        raise AssertionError('unexpected fixture document')
    fake = SimpleNamespace(db=SimpleNamespace(exists=lambda dt, name: existing if dt in {'Company', 'User'} else True),
        get_doc=doc)
    fixtures = SimpleNamespace(install=lambda **kwargs: events.append('fixtures') if kwargs == {'country': 'China'} else pytest.fail('wrong country'))
    monkeypatch.setitem(sys.modules, 'frappe', fake)
    monkeypatch.setitem(sys.modules, 'frappe.permissions', SimpleNamespace(add_permission=None, update_permission_property=None))
    monkeypatch.setitem(sys.modules, 'frappe.custom.doctype.custom_field.custom_field', SimpleNamespace(create_custom_fields=None))
    monkeypatch.setitem(sys.modules, 'erpnext.setup.setup_wizard.operations', SimpleNamespace(install_fixtures=fixtures))
    rt = runtime()
    rt.lab_session = lambda: nullcontext(fake)
    monkeypatch.setitem(sys.modules, 'lab_runtime', rt)
    # Match Python's script-directory import path used by the real container.
    monkeypatch.syspath_prepend(str(ROOT / 'integrations/erpnext/sandbox'))
    spec = importlib.util.spec_from_file_location('seed_reference_test', ROOT / 'integrations/erpnext/sandbox/seed.py')
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    if existing:
        with pytest.raises(RuntimeError, match='REFUSE_ALREADY_SEEDED_LAB'):
            seed.main()
        assert events == []
    else:
        with pytest.raises(StopAfterCompany):
            seed.main()
        assert events == ['fixtures']
