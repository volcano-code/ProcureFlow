"""Fixed-site, fail-closed runtime and bounded diagnostics for the disposable lab."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import re
import traceback

SITE = 'pf-erp-test.local'
SITES = Path('/home/frappe/frappe-bench/sites')
PHASE = 'initialization'
CODES = frozenset({'EPHEMERAL_LAB_ONLY', 'LAB_MARKER_MISMATCH',
    'REFUSE_ALREADY_SEEDED_LAB', 'DRAFT_ROLE_NOT_RESTRICTED', 'UNEXPECTED_PO_PERMISSION'})


def phase(name):
    global PHASE
    if not re.fullmatch(r'[a-z][a-z_-]{0,47}', name):
        raise ValueError('invalid diagnostic phase')
    PHASE = name


@contextmanager
def lab_session():
    """Match bench's sites working directory before Frappe opens ../logs/*.log."""
    nonce = os.environ.get('PF_EPHEMERAL_NONCE', '')
    if os.environ.get('PF_EPHEMERAL_ERP') != '1' or not re.fullmatch(r'[0-9a-f]{64}', nonce):
        raise RuntimeError('EPHEMERAL_LAB_ONLY')
    import frappe
    previous = Path.cwd()
    initialized = False
    try:
        phase('site-context')
        os.chdir(SITES)
        initialized = True
        frappe.init(site=SITE, sites_path=str(SITES))
        # Check the locally installed marker BEFORE opening the database.
        if frappe.conf.get('procureflow_test_nonce') != nonce:
            raise RuntimeError('LAB_MARKER_MISMATCH')
        phase('database-connect')
        frappe.connect()
        frappe.set_user('Administrator')
        yield frappe
    finally:
        try:
            if initialized:
                frappe.destroy()
        finally:
            os.chdir(previous)


class _Discard:
    """Do not retain or publish Frappe stdout, document values, or credentials."""
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def failure(error):
    kind = type(error).__name__
    frames = traceback.extract_tb(error.__traceback__)[-8:]
    return {'status': 'failed', 'phase': PHASE,
        'error_type': kind if re.fullmatch(r'[A-Za-z_]{1,64}', kind) else 'Exception',
        'reason': str(error) if type(error) is RuntimeError and str(error) in CODES else 'STAGE_FAILED',
        # No message, source text, absolute path, request body, or frame locals.
        'frames': [{'file': Path(f.filename).name if re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', Path(f.filename).name) else 'unknown',
                    'line': f.lineno} for f in frames]}


def run_stage(stage, function):
    if stage not in {'seed', 'database-audit'}:
        raise ValueError('unknown lab stage')
    phase('initialization')
    try:
        with redirect_stdout(_Discard()), redirect_stderr(_Discard()):
            result = function()
        report = {'stage': stage, 'status': 'passed', **(result or {})}
        code = 0
    except Exception as error:
        report = {'stage': stage, **failure(error)}
        code = 1
    print(json.dumps(report, indent=2))
    return code
