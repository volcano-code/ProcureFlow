"""Opt-in native pilot login -> browser tables -> independent Worker acceptance.

Actual ERP execution is confined to the existing new marked loopback sandbox.
--fixture instead runs the same browser/lifecycle path with a persisted mock ERP;
it is never accepted as actual-ERP evidence. No credentials, browser traces, HTML
or raw exception/server output are published. No live model is configured.
"""
from __future__ import annotations
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'apps/web'
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'services/api'),
               str(ROOT / 'integrations/erpnext/sandbox'), str(WEB / 'e2e')]
from erp_business_database import business_database, postgres_test_url
from erp_tabular_fixtures import assert_provenance, save_source_files
from cost_fixtures import ACCEPTANCE_CASES, TAX_ACCOUNT, FREIGHT_ACCOUNT
from verify_erp_sandbox import fault_proxy, validate_lab
from pilot_gate_contract import SCENARIOS, DENIALS, STEPS


def isolated_environment(directory, database_url, api_url, web_url, data, erp_url, fixture):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PF_', 'ERP_', 'LLM_', 'NEXT_', 'PG'))
           and key not in {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'}}
    env.update(PYTHONPATH=str(ROOT / 'services/api'), PF_DATA_DIR=str(directory),
        PF_DATABASE_URL=database_url, PF_MODE='pilot', PF_ERP_MODE='mock' if fixture else 'erpnext',
        ERP_ALLOW_DRAFT_WRITES='false' if fixture else 'true', NEXT_TELEMETRY_DISABLED='1',
        NEXT_PUBLIC_API_BASE_URL='/backend', PF_INTERNAL_API_ORIGIN=api_url, PF_WEB_ORIGINS=web_url)
    if not fixture:
        env.update(ERP_BASE_URL=erp_url, ERP_COMPANY=data['company'], ERP_API_KEY=data['api_key'],
            ERP_API_SECRET=data['api_secret'], ERP_TAX_ACCOUNT=TAX_ACCOUNT, ERP_FREIGHT_ACCOUNT=FREIGHT_ACCOUNT)
    return env


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


def run_checked(args, env, cwd=ROOT, timeout=180):
    result = subprocess.run(args, env=env, cwd=cwd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    if result.returncode:
        raise RuntimeError('PILOT_CHILD_PROCESS_FAILED')


def start(args, env, url, cwd=ROOT):
    process = subprocess.Popen(args, env=env, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(250):
        if process.poll() is not None: break
        try:
            if httpx.get(url, timeout=.4, trust_env=False).status_code == 200:
                return process
        except httpx.HTTPError: pass
        time.sleep(.1)
    stop(process)
    raise RuntimeError('PILOT_SERVER_START_FAILED')


def audit_business(url, operations, denials, backend, fixture=False):
    """Independent SQL read, after restart, never service DTOs or ORM state."""
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as db:
            from alembic.script import ScriptDirectory
            assert set(db.scalars(text('SELECT version_num FROM alembic_version'))) == set(
                ScriptDirectory(str(ROOT / 'services/api/alembic')).get_heads())
            rows = db.execute(text('SELECT id, tenant_id, status, snapshot_hash, remote_id, error, attempts, '
                'approval_id, initiator_id, initiator_auth_version FROM external_operations')).mappings().all()
            approvals = db.execute(text('SELECT id, approver_id, approver_auth_version FROM approvals')).mappings().all()
            assert len(rows) == len(SCENARIOS) + len(DENIALS)
            for expected in operations + denials:
                row, = [row for row in rows if row['id'] == expected['operation_id']]
                assert row['tenant_id'] == 'lab' and row['snapshot_hash'] == expected['snapshot_hash']
                assert row['status'] == expected['final_status'] and row['remote_id'] == expected['remote_id']
                approval, = [item for item in approvals if item['id'] == row['approval_id']]
                assert row['initiator_id'] and approval['approver_id'] != row['initiator_id']
                assert row['initiator_auth_version'] and approval['approver_auth_version']
                if expected in denials:
                    assert row['attempts'] == 0 and row['error'] == expected['denial_reason']
            outbox = db.execute(text('SELECT operation_id, status FROM outbox')).mappings().all()
            assert len(outbox) == len(rows) and all(row['status'] == 'DONE' for row in outbox)
            assert {r['operation_id'] for r in outbox} == {r['id'] for r in rows}
            receipts = db.execute(text("SELECT payload FROM audit_events WHERE type = 'ERP_VERIFICATION_VERIFIED'")).scalars().all()
            receipts = [json.loads(value) if isinstance(value, str) else value for value in receipts]
            assert len(receipts) == 2
            for expected in operations:
                receipt, = [item for item in receipts if item['operation_id'] == expected['operation_id']]
                assert receipt['remote_id'] == expected['remote_id'] and receipt['snapshot_hash'] == expected['snapshot_hash']
                assert receipt['simulated'] is fixture and receipt['external_write_attempted'] is False
            sessions = db.scalar(text('SELECT count(*) FROM pilot_sessions'))
            memberships = db.scalar(text('SELECT count(*) FROM pilot_memberships'))
            assert sessions >= 10 and memberships >= 10
            result = {'status': 'passed', 'database': backend, 'read_only_audit': True,
                'after_api_restart': True, 'migration_current': True, 'operation_identity_matches': True,
                'operation_count': 5, 'completed_operation_count': 2, 'denied_operation_count': 3,
                'done_outbox_count': 5, 'durable_pilot_identity_rows': True,
                'buyer_approver_binding_checked': True, 'denied_dispatch_attempts': 0, 'verified_receipt_count': 2}
            if backend == 'postgresql':
                import re
                assert re.fullmatch('pf_erp_test_[0-9a-f]{32}', db.scalar(text('SELECT current_schema()')))
                result.update(isolated_schema=True, server_version_num=db.scalar(text("SELECT current_setting('server_version_num')")))
            return result
    finally: engine.dispose()


def exercise(data, output, backend='sqlite', fixture=False):
    from playwright.sync_api import sync_playwright
    from pilot_combined import prepare_case, logout
    from procureflow.config import Settings
    from procureflow.db import Database, PilotSessionRow, PilotMembershipRow
    from procureflow.auth import IdentityService

    report = {'scope': 'native pilot browser and independent Worker in disposable synthetic lab',
        'status': 'running', 'synthetic_only': True, 'real_user_account_used': False,
        'human_approval_measured': False, 'model_used': False, 'fixture': fixture,
        'live_erp': not fixture, 'browser_verified': False, 'api_business_database': backend, 'steps': [], 'operations': [], 'denials': []}
    phase = 'initialization'
    with tempfile.TemporaryDirectory(prefix='pf-pilot-combined-') as directory, \
            business_database(backend, directory) as database_url, \
            (nullcontext(('', {'posts': [], 'requests': []})) if fixture else fault_proxy()) as (erp_url, wire):
        api_port, web_port = free_port(), free_port()
        while api_port == web_port: web_port = free_port()
        api_url, web_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{web_port}'
        env = isolated_environment(directory, database_url, api_url, web_url, data, erp_url, fixture)
        api = web = db = None
        phase = 'migrations'
        try:
            run_checked([sys.executable, '-m', 'alembic', 'upgrade', 'head'], env, ROOT / 'services/api')
            from unittest.mock import patch
            with patch.dict(os.environ, env, clear=True):
                settings = Settings(data_dir=Path(directory), database_url=database_url, mode='pilot', auth_tokens={},
                    erp_mode='mock', erp_allow_draft_writes=False)
            db = Database(database_url)
            identities = IdentityService(db, settings)
            identities.create_tenant('lab')
            phase = 'native-build'
            # ERP/session secrets are not inherited by the frontend compiler or process.
            node_env = {k: v for k, v in env.items() if not k.startswith(('ERP_', 'LLM_'))
                        and k not in {'PF_DATABASE_URL', 'PF_DATA_DIR'}}
            run_checked(['npm', 'run', 'build'], node_env, WEB, timeout=300)
            report['steps'].append(STEPS[0])
            api_args = [sys.executable, str(ROOT / 'scripts/start.py'), '--port', str(api_port)]
            api = start(api_args, env, api_url + '/ready')
            assert httpx.get(api_url + '/ready', trust_env=False).json()['database'] == backend
            web = start(['node', str(WEB / 'node_modules/next/dist/bin/next'), 'start', '--hostname',
                '127.0.0.1', '--port', str(web_port)], node_env, web_url, WEB)
            def restart_api():
                nonlocal api
                stop(api); api = start(api_args, env, api_url + '/ready')
            def worker(lose=None):
                if fixture and lose:
                    # A separate actual Worker process using the documented mock receipt-loss fixture.
                    run_checked([sys.executable, '-c', 'import sys; from procureflow.app import app; '
                        'from procureflow.worker import drain_once; '
                        'app.state.service.erp.fail_after_commit_once.add(sys.argv[1]); '
                        'drain_once(app.state.service)', lose], env, timeout=90)
                else:
                    run_checked([sys.executable, '-m', 'procureflow.worker', '--once'], env, timeout=90)
            def drafts():
                if not fixture: return len(wire['posts'])
                import sqlite3
                with sqlite3.connect(Path(directory) / 'mock-erp.sqlite3') as conn:
                    return conn.execute('SELECT count(*) FROM drafts').fetchone()[0]
            def call(token, method, path, expected=200, **kwargs):
                response = httpx.request(method, api_url + '/api/v1' + path,
                    headers={'Authorization': 'Bearer ' + token}, timeout=30, trust_env=False, **kwargs)
                assert response.status_code == expected, 'PILOT_READBACK_FAILED'
                return response.json()
            if not fixture:
                phase = 'erp-preflight'
                from procureflow.erp import ERPNextClient
                adapter = ERPNextClient(erp_url, data['api_key'], data['api_secret'], data['company'],
                    tax_account=TAX_ACCOUNT, freight_account=FREIGHT_ACCOUNT)
                try: report['preflight'] = adapter.preflight(expected_user=data['user'])
                finally: adapter.client.close()
            report['steps'].append('mock_erp_no_real_preflight' if fixture else STEPS[1])
            phase = 'browser-launch'
            with sync_playwright() as playwright:
                executable = os.getenv('PF_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
                try:
                    browser = playwright.chromium.launch(executable_path=executable, headless=True,
                        args=['--no-sandbox', '--disable-dev-shm-usage'])
                except Exception as error:
                    reason = ('CHROMIUM_SYSTEM_SOCKET_DENIED' if 'Operation not permitted' in str(error)
                              else 'CHROMIUM_LAUNCH_FAILED')
                    raise RuntimeError(reason) from None
                try:
                    cases = [(scenario, None) for scenario in SCENARIOS] + [(SCENARIOS[0], denial) for denial in DENIALS]
                    for index, (scenario, denial) in enumerate(cases):
                        phase = denial or scenario
                        users = {role: f'pilot-{index}-{role}' for role in ('buyer', 'approver')}
                        invitations = {}
                        for role, user in users.items():
                            identities.set_membership('lab', user, role)
                            invitations[role] = identities.issue_invite('lab', user)['credential']
                        before = drafts()
                        case = prepare_case(browser, web_url, invitations, data, scenario, 'Synthetic pilot ' + str(index))
                        try:
                            assert drafts() == before, 'BROWSER_PERFORMED_ERP_WRITE'
                            op = case['operation']; token = case['buyer_session']['token']
                            observer = case['approver_session']['token']
                            invalid_token = token
                            if denial == 'buyer-revocation':
                                identities.set_membership('lab', users['buyer'], 'buyer', active=False)
                            elif denial == 'approver-revocation':
                                invalid_token, observer = observer, token
                                identities.set_membership('lab', users['approver'], 'approver', active=False)
                            elif denial == 'approver-membership-expiry':
                                invalid_token, observer = observer, token
                                with db.transaction(write=True) as session:
                                    # Test expiry itself, without a generation change masking it.
                                    session.get(PilotMembershipRow, ('lab', users['approver'])).expires_at = '2000-01-01T00:00:00+00:00'
                            if denial:
                                call(invalid_token, 'GET', '/me', expected=401)
                                restart_api(); worker(); worker()
                                final = call(observer, 'GET', '/operations/' + op['id'])
                                assert final['status'] == 'NEEDS_HUMAN' and final['remote_id'] is None
                                assert final['error'] == ('BUYER_REVOKED' if denial == 'buyer-revocation' else 'APPROVER_REVOKED')
                                assert drafts() == before
                                report['denials'].append({'scenario': denial, 'operation_id': op['id'],
                                    'snapshot_hash': op['snapshot_hash'], 'final_status': final['status'], 'remote_id': None,
                                    'denial_reason': final['error'],
                                    'stale_session_http_status': 401, 'api_restarted': True, 'fresh_workers': 2,
                                    'first_write_attempts': 0, 'browser_checks': case['browser_checks']})
                                report['steps'].append(STEPS[4 + DENIALS.index(denial)])
                                continue
                            lost = ACCEPTANCE_CASES[scenario]['lose_receipt']
                            if not lost:
                                # An accepted outbox intent survives browser-session expiry while
                                # its buyer/approver membership authority remains valid.
                                principal = identities.authenticate(token)
                                with db.transaction(write=True) as session:
                                    session.get(PilotSessionRow, principal.session_id).expires_at = '2000-01-01T00:00:00+00:00'
                                call(token, 'GET', '/me', expected=401)
                                call(token, 'POST', '/operations/' + op['id'] + '/process', expected=401)
                            if not fixture: wire['lose_next'] = lost
                            worker(op['id'] if lost else None)
                            first = call(observer, 'GET', '/operations/' + op['id'])
                            if lost:
                                assert first['status'] == 'RECONCILING'
                                logout(case['buyer'])
                                call(token, 'GET', '/me', expected=401)
                                call(token, 'POST', '/operations/' + op['id'] + '/process', expected=401)
                                restart_api()
                                read_start = len(wire['requests'])
                                worker(); worker()
                                if not fixture:
                                    recovery = wire['requests'][read_start:]
                                    assert recovery and all(item['method'] == 'GET' for item in recovery)
                            final = call(observer, 'GET', '/operations/' + op['id'])
                            assert final['status'] == 'COMPLETED' and bool(final['remote_id'])
                            assert final['remote_id'].startswith('MOCK-') is fixture
                            receipt = call(observer, 'POST', '/operations/' + op['id'] + '/verify')
                            assert receipt['status'] == 'verified' and receipt['simulated'] is fixture
                            assert receipt['external_write_attempted'] is False
                            worker()
                            assert drafts() == before + 1
                            if not fixture: assert len([x for x in wire['posts'] if x['key'] == op['id']]) == 1
                            document = call(observer, 'GET', '/documents/' + case['quote']['document_id'] + '/evidence')
                            provenance = assert_provenance(data, scenario, case['content'], case['mapped'], case['quote'], document)
                            report['operations'].append({'scenario': scenario, 'operation_id': op['id'],
                                'snapshot_hash': op['snapshot_hash'], 'remote_id': final['remote_id'], 'final_status': 'COMPLETED',
                                'expected_total': ACCEPTANCE_CASES[scenario]['total'],
                                'input_format': ACCEPTANCE_CASES[scenario]['input_format'], 'provenance': provenance,
                                'browser_checks': case['browser_checks'], 'worker_process_independent': True,
                                'post_attempts': 1 if not fixture else None, 'cost_components_verified': True,
                                'tabular_provenance_verified': True, 'readback_verified': True,
                                'logout_before_recovery': lost, 'recovery_read_only': lost, 'api_restarted_before_recovery': lost,
                                'session_expired_after_enqueue': not lost, 'stale_session_denied': True,
                                'stale_session_mutation_denied': True,
                                'accepted_work_survives_session_expiry': not lost})
                            report['steps'].append(STEPS[3 if lost else 2])
                        finally:
                            for context in case['contexts']: context.close()
                finally: browser.close()
            phase = 'business-database-audit'
            restart_api()
            report['business_database_audit'] = audit_business(database_url, report['operations'], report['denials'], backend, fixture)
            report['steps'].append(STEPS[7])
            report.update(status='passed', browser_verified=True, post_attempts=2 if not fixture else None,
                real_erpnext_drafts_verified=0 if fixture else 2, mock_drafts_verified=2 if fixture else 0,
                real_erp_database=None if fixture else 'MariaDB')
        except Exception as error:
            # Never print invitation/session credentials, exception messages or captured HTML.
            import traceback
            frames = traceback.extract_tb(error.__traceback__)[-5:]
            blocked = phase == 'browser-launch' and str(error) in {'CHROMIUM_SYSTEM_SOCKET_DENIED', 'CHROMIUM_LAUNCH_FAILED'}
            report.update(status='blocked' if blocked else 'failed', phase=phase,
                reason=str(error) if blocked else 'PILOT_COMBINED_GATE_FAILED',
                diagnostic_frames=[{'file': Path(frame.filename).name, 'line': frame.lineno} for frame in frames])
        finally:
            stop(web); stop(api)
            if db: db.engine.dispose()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--ephemeral-test', action='store_true')
    mode.add_argument('--fixture', action='store_true')
    parser.add_argument('--business-database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--credentials', type=Path, default=Path('.data/erp-sandbox.json'))
    parser.add_argument('--env-file', type=Path, default=Path('.env.erp-sandbox'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = {'status': 'blocked', 'network_attempted': False, 'reason': 'EXPLICIT_EPHEMERAL_TEST_REQUIRED'}
    code = 2
    try:
        if sys.flags.optimize: raise ValueError('OPTIMIZED_PYTHON_NOT_SUPPORTED')
        if not (args.ephemeral_test or args.fixture): raise ValueError('EXPLICIT_EPHEMERAL_TEST_REQUIRED')
        data = ({'supplier': 'SUP-A', 'sku': 'PF-SANDBOX-ITEM'} if args.fixture
                else validate_lab(args.credentials, args.env_file))
        if args.business_database == 'postgresql': postgres_test_url()
        if not shutil.which('node') or not shutil.which('npm') or not (WEB / 'node_modules/next/dist/bin/next').is_file():
            raise ValueError('NEXT_DEPENDENCIES_MISSING')
    except (ValueError, TypeError, KeyError, OSError) as error:
        report['reason'] = str(error) if str(error) in {'OPTIMIZED_PYTHON_NOT_SUPPORTED',
            'EXPLICIT_EPHEMERAL_TEST_REQUIRED', 'NEXT_DEPENDENCIES_MISSING'} else 'LAB_CONFIGURATION_MISMATCH'
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        save_source_files(args.output.parent, data)
        try:
            report = exercise(data, args.output.parent, args.business_database, args.fixture)
            code = 0 if report['status'] == 'passed' else 2 if report['status'] == 'blocked' else 1
        except Exception:
            report = {'status': 'failed', 'reason': 'PILOT_COMBINED_GATE_FAILED', 'fixture': args.fixture}
            code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__': raise SystemExit(main())
