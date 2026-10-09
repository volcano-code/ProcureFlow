"""Strict offline acceptance: synthetic procurement -> paired backup -> fresh paused restore.

Always allocates temporary source/target directories. No caller DB/ERP/model URL,
credentials, accounts or document directories are consumed. No backup data leaves
this process; output contains only synthetic hashes/counts and verified outcomes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))


def run(output, protection="plain"):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from procureflow.app import create_app
    from procureflow.auth import IdentityService
    from procureflow.backup import _migrate
    from procureflow.backup_protection import create_backup, restore_archive, verify_archive
    from jwcrypto import jwk
    from procureflow.config import Settings, DEMO_IDENTITIES
    from procureflow.db import Base, Database, TableImportRow
    from procureflow.domain import digest
    from procureflow.erp import MockERP
    from procureflow.errors import DomainError
    from procureflow.maintenance import pause_writes, recovery_report, resume_writes
    from procureflow.retention import RetentionService
    from procureflow.recovery import offline_recovery_detail
    from procureflow.worker import drain_once

    report = {'scope': 'synthetic SQLite paired recovery acceptance', 'status': 'failed',
              'real_user_data': False, 'live_erp': False, 'live_model': False,
              'external_writes': 0, 'automatic_replay': False}
    # Ephemeral synthetic material only: never persisted, exported to reports or
    # reused as an identity. Production key provisioning is operator-owned.
    encrypted, signed = 'encrypted' in protection, 'signed' in protection
    encryption_key = jwk.JWK.generate(kty='oct', size=256).export().encode() if encrypted else None
    signer = jwk.JWK.generate(kty='OKP', crv='Ed25519') if signed else None
    signing_key = signer.export_private().encode() if signer else None
    verification_key = signer.export_public().encode() if signer else None
    create_keys = dict(mode=protection, encryption_key=encryption_key, signing_key=signing_key)
    verify_keys = dict(mode=protection, encryption_key=encryption_key, verification_key=verification_key)
    buyer = {'Authorization': 'Bearer demo-buyer'}
    approver = {'Authorization': 'Bearer demo-approver'}
    databases = []
    try:
        with tempfile.TemporaryDirectory(prefix='pf-recovery-acceptance-') as temporary:
            root = Path(temporary)
            data = root / 'source'
            settings = Settings(data_dir=data, database_url=f'sqlite:///{data / "business.sqlite3"}',
                mode='demo', auth_tokens={k: dict(v) for k, v in DEMO_IDENTITIES.items()},
                erp_mode='mock', erp_allow_draft_writes=False, erp_api_key='', erp_api_secret='',
                erp_url='', erp_company='')
            db = Database(settings.database_url)
            databases.append(db)
            _migrate(db)
            erp = MockERP(data / 'mock-erp.sqlite3')
            app = create_app(settings, db, erp)
            with TestClient(app) as client:
                def post(path, body=None, headers=buyer, **kwargs):
                    response = client.post(path, json=body, headers=headers, **kwargs)
                    assert response.status_code in (200, 201, 202), f'HTTP_{response.status_code}'
                    return response.json()
                request = post('/api/v1/requests', {'title': 'Synthetic recovery acceptance', 'sku': 'STAND-01',
                    'quantity': '20', 'budget': '30000.00', 'max_delivery_days': 14})
                rid = request['id']
                quote = post(f'/api/v1/requests/{rid}/documents', files={
                    'file': ('supplier-a.txt', (ROOT / 'evals/fixtures/supplier-a.txt').read_bytes())})
                post(f'/api/v1/quotes/{quote["id"]}/confirm', {'expected_version': 1, 'acknowledge': True})
                # A separate ordinary table preview expires and is archived without deleting bytes.
                preview = post(f'/api/v1/requests/{rid}/table-imports', files={
                    'file': ('supplier-table.csv', (ROOT / 'evals/fixtures/tabular/supplier-table.csv').read_bytes())})
                with db.transaction(write=True) as session:
                    session.get(TableImportRow, preview['id']).expires_at = '2000-01-01T00:00:00+00:00'
                retention = RetentionService(db)
                plan = retention.plan('demo')
                assert len(plan['entries']) == 1
                assert retention.apply(plan, tenant_id='demo', actor='synthetic-operator')['changed'] == [preview['id']]
                analyzed = post(f'/api/v1/requests/{rid}/analyze', {})
                proposal = analyzed['proposal']
                post(f'/api/v1/requests/{rid}/approval', {'snapshot_hash': proposal['snapshot_hash']}, headers=approver)
                operation = post(f'/api/v1/requests/{rid}/execute', {'snapshot_hash': proposal['snapshot_hash']})
                # Synthetic pilot material is kept in memory and temp DB only.
                pilot_settings = Settings(data_dir=data, database_url=settings.database_url, mode='pilot',
                    auth_tokens={}, erp_mode='mock', erp_allow_draft_writes=False, erp_api_key='', erp_api_secret='')
                identity = IdentityService(db, pilot_settings)
                identity.create_tenant('synthetic-pilot')
                identity.set_membership('synthetic-pilot', 'auditor', 'auditor')
                invite = identity.issue_invite('synthetic-pilot', 'auditor')
                token = identity.login(invite['credential'])['token']
                identity.issue_invite('synthetic-pilot', 'auditor')
                pause_writes(db)
                backup_path = root / 'paired.pfb'
                backup = create_backup(db, data / 'documents', backup_path, **create_keys)
                verified = verify_archive(backup_path, **verify_keys)
                restored = restore_archive(backup_path, root / 'restored', **verify_keys)
                recovered = Database(f'sqlite:///{restored.data_dir / "procureflow.sqlite3"}')
                databases.append(recovered)
                restored_settings = Settings(data_dir=restored.data_dir,
                    database_url=recovered.engine.url.render_as_string(hide_password=False), mode='pilot',
                    auth_tokens={}, erp_mode='mock', erp_allow_draft_writes=False,
                    erp_api_key='', erp_api_secret='', erp_url='', erp_company='')
                restored_erp = MockERP(root / 'isolated-mock-erp.sqlite3')
                restored_app = create_app(restored_settings, recovered, restored_erp)
                assert drain_once(restored_app.state.service) == 0 and restored_erp.count() == 0
                with TestClient(restored_app) as recovered_client:
                    assert recovered_client.post('/api/v1/auth/login', json={'credential': invite['credential']}).status_code == 503
                    assert recovered_client.get('/api/v1/recovery/operations', headers={
                        'Authorization': 'Bearer ' + token}).status_code == 401
                try:
                    IdentityService(recovered, restored_settings).authenticate(token)
                    raise AssertionError('OLD_SESSION_ACCEPTED')
                except DomainError as error:
                    assert error.code == 'UNAUTHENTICATED'
                preserved = {}
                with recovered.transaction() as session:
                    for table in Base.metadata.sorted_tables:
                        original = verified.tables[table.name]
                        rows = [dict(row) for row in session.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
                        if table.name == 'audit_events':
                            by_id = {row['id']: row for row in rows}
                            assert all(by_id[row['id']] == row for row in original)
                            preserved[table.name] = digest(original)
                        elif table.name not in {'system_state', 'recovery_holds', 'pilot_tenants',
                                               'pilot_memberships', 'pilot_invites', 'pilot_sessions'}:
                            assert rows == original, 'EVIDENCE_MISMATCH_' + table.name
                            preserved[table.name] = digest(rows)
                document_hashes = {}
                for key, metadata in verified.manifest['documents'].items():
                    actual = hashlib.sha256((restored.data_dir / 'documents' / key).read_bytes()).hexdigest()
                    assert actual == metadata['sha256']
                    document_hashes[key] = actual
                inventory = recovery_report(recovered)
                assert inventory['state'] == 'RECOVERY'
                assert inventory['operations'][0]['hold_restore_id'] == backup['backup_id']
                assert inventory['operations'][0]['status'] == 'PENDING'
                offline_detail = offline_recovery_detail(recovered, restored.data_dir / 'documents', 'demo', operation['id'])
                assert offline_detail['sources'][0]['integrity'] == 'verified'
                assert offline_detail['replay_permitted'] is False
                assert recovered.state()['state'] == 'RECOVERY'
                resume_writes(recovered, generation=inventory['generation'], ledger_sha256=inventory['ledger_sha256'],
                    restore_id=backup['backup_id'], acknowledge_reconciliation=True, acknowledge_credentials=True)
                assert drain_once(restored_app.state.service) == 0 and restored_erp.count() == 0
                try:
                    restored_app.state.service.process_pending_operation('demo', operation['id'])
                    raise AssertionError('RESTORED_REPLAY_ALLOWED')
                except DomainError as error:
                    assert error.code == 'RECOVERY_OPERATION_HELD'
                # Fresh auditor authority may inspect an old uncertain operation
                # through the existing GET-only ERP verification path. The hold
                # never blocks evidence access, and verification cannot POST.
                recovery_identity = IdentityService(recovered, restored_settings)
                recovery_identity.create_tenant('demo')
                recovery_identity.set_membership('demo', 'recovery-auditor', 'auditor')
                fresh = recovery_identity.issue_invite('demo', 'recovery-auditor')
                fresh_token = recovery_identity.login(fresh['credential'])['token']
                auditor = recovery_identity.authenticate(fresh_token)
                receipt = restored_app.state.service.verify_operation(auditor, operation['id'])
                assert receipt['status'] == 'missing' and receipt['external_write_attempted'] is False
                assert restored_erp.count() == 0
                after_verify = recovery_report(recovered)['operations'][0]
                assert after_verify['status'] == 'PENDING' and after_verify['attempts'] == 0
                assert after_verify['hold_restore_id'] == backup['backup_id']
                def database_digest():
                    with recovered.transaction() as session:
                        return digest({table.name: [dict(row) for row in session.execute(
                            select(table).order_by(*table.primary_key.columns)).mappings()]
                            for table in Base.metadata.sorted_tables})
                before_diagnostics = database_digest()
                with TestClient(restored_app) as recovered_client:
                    auth = {'Authorization': 'Bearer ' + fresh_token}
                    listing = recovered_client.get('/api/v1/recovery/operations', headers=auth)
                    assert listing.status_code == 200 and listing.json()['total'] == 1
                    detail = recovered_client.get('/api/v1/recovery/operations/' + operation['id'], headers=auth)
                    assert detail.status_code == 200 and detail.json()['sources'][0]['integrity'] == 'verified'
                    readback = recovered_client.get('/api/v1/recovery/operations/' + operation['id'] + '/reconciliation', headers=auth)
                    assert readback.status_code == 200 and readback.json()['status'] == 'missing'
                    assert readback.json()['reason'] == 'REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT'
                    assert readback.json()['replay_permitted'] is False
                assert database_digest() == before_diagnostics and restored_erp.count() == 0
                report.update(status='passed', backup_id=backup['backup_id'],
                    backup_protection=verified.protection,
                    source_fingerprint=verified.manifest['source']['fingerprint'],
                    schema_heads=verified.manifest['schema_heads'], documents_verified=len(document_hashes),
                    document_sha256=document_hashes, preserved_table_sha256=preserved,
                    old_session_rejected=True, api_writes_paused=True, worker_paused=True,
                    reviewed_resume_keeps_operations_held=True, archived_source_preserved=True,
                    held_operation_read_only_verification=True,
                    recovery_diagnostics_gets_leave_database_unchanged=True,
                    offline_diagnostics_while_recovery_paused=True,
                    restored_credentials_cannot_read_diagnostics=True,
                    source_remains_paused=db.state()['state'] == 'PAUSED',
                    production_backup_or_restore_performed=False)
        return 0
    except Exception:
        # Neither a failed assertion nor traceback may publish fixture credentials.
        report['reason'] = 'SYNTHETIC_RECOVERY_ACCEPTANCE_FAILED'
        return 1
    finally:
        for database in databases:
            database.engine.dispose()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'evals/reports/recovery/acceptance.json')
    parser.add_argument('--protection', choices=('plain', 'signed', 'encrypted', 'signed-encrypted'),
                        default='plain', help='Explicit synthetic profile, legacy default retained for existing CI')
    args = parser.parse_args()
    # Remove ambient service secrets before any module-level default app is imported.
    for key in list(os.environ):
        if key.startswith(('PF_', 'ERP_', 'LLM_')):
            os.environ.pop(key, None)
    with tempfile.TemporaryDirectory(prefix='pf-recovery-module-') as temporary:
        os.environ.update(PF_DATA_DIR=temporary, PF_MODE='demo', PF_ERP_MODE='mock', ERP_ALLOW_DRAFT_WRITES='false')
        raise SystemExit(run(args.output.resolve(), args.protection))
