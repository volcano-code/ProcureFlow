"""Offline multi-item acceptance, using only temporary SQLite and synthetic CSVs."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(output: Path) -> int:
    # Importing app creates its default instance. Override every relevant setting
    # before importing it; the acceptance process never consumes ambient targets.
    report = {"scope": "synthetic multi-item SQLite + MockERP acceptance", "status": "failed",
              "live_erp": False, "live_model": False, "production_writes": 0}
    with tempfile.TemporaryDirectory(prefix='pf-multi-item-') as directory:
        for key in list(os.environ):
            if key.startswith(('PF_', 'ERP_', 'LLM_')):
                os.environ.pop(key)
        os.environ.update(PF_DATA_DIR=directory, PF_MODE='demo', PF_ERP_MODE='mock', ERP_ALLOW_DRAFT_WRITES='false')
        sys.path.insert(0, str(ROOT / 'services/api'))
        from fastapi.testclient import TestClient
        from procureflow.app import app
        from procureflow.db import RecoveryHoldRow, now
        buyer = {'Authorization': 'Bearer demo-buyer'}
        approver = {'Authorization': 'Bearer demo-approver'}
        auditor = {'Authorization': 'Bearer demo-auditor'}
        try:
            with TestClient(app) as client:
                def post(path, body=None, headers=buyer, **kwargs):
                    response = client.post('/api/v1' + path, json=body, headers=headers, **kwargs)
                    assert response.status_code in (200, 201, 202), f'HTTP_{response.status_code}'
                    return response.json()
                def request():
                    return post('/requests', json.loads((ROOT / 'evals/fixtures/multi-item/request.json').read_text()))
                def table(req, supplier, rows):
                    imported = post(f'/requests/{req["id"]}/table-imports', files={'file':
                        (supplier + '.csv', (ROOT / f'evals/fixtures/multi-item/{supplier}.csv').read_bytes())})
                    preview = post(f'/table-imports/{imported["id"]}/preview', {'expected_revision':imported['revision'],
                        'sheet': imported['sheets'][0]['name'], 'header_row':1, 'rows':rows,
                        'mapping': {key: chr(65 + index) for index,key in enumerate(
                            ['supplier_id','sku','quantity','uom','unit_price','tax_mode','tax_rate','shipping_cost','discount','delivery_days','currency'])}})
                    quote = post(f'/table-imports/{imported["id"]}/confirm',
                                 {'expected_revision':preview['revision'],'acknowledge':True})
                    assert quote['confirmed_by'] is None
                    assert f'lines.{len(rows)-1}.unit_price' in quote['evidence']
                    return post(f'/quotes/{quote["id"]}/confirm', {'expected_version':quote['version'],'acknowledge':True})
                req = request()
                a, b = table(req,'supplier-a',[2,3]), table(req,'supplier-b',[2,3])
                result = post(f'/requests/{req["id"]}/analyze', {})
                proposal = result['proposal']
                assert proposal['quote_id'] == a['id'] and proposal['total'] == '233.00'
                assert b['calculation']['total'] == '247.00' and result['valid_quote_count'] == 2
                post(f'/requests/{req["id"]}/approval', {'snapshot_hash':proposal['snapshot_hash']}, headers=approver)
                operation = post(f'/requests/{req["id"]}/execute', {'snapshot_hash':proposal['snapshot_hash']})
                erp = app.state.service.erp
                erp.fail_after_commit_once.add(operation['id'])
                assert post(f'/operations/{operation["id"]}/process')['status'] == 'RECONCILING'
                assert erp.count() == 1
                assert post(f'/operations/{operation["id"]}/process')['status'] == 'COMPLETED'
                assert post(f'/operations/{operation["id"]}/verify', headers=auditor)['status'] == 'verified'
                assert post(f'/requests/{req["id"]}/execute', {'snapshot_hash':proposal['snapshot_hash']})['id'] == operation['id']
                assert erp.count() == 1
                partial = request(); quote = table(partial,'supplier-a',[2])
                assert not quote['calculation']['eligible']
                assert quote['calculation']['coverage']['missing_skus'] == ['CABLE-02']
                assert post(f'/requests/{partial["id"]}/analyze', {})['proposal'] is None
                # Read-only recovered holds cannot confer permission to repeat writes.
                with app.state.service.db.transaction(write=True) as session:
                    session.add(RecoveryHoldRow(operation_id=operation['id'], restore_id='synthetic-multi-check',
                        original_status='IN_FLIGHT', created_at=now()))
                response = client.get(f'/api/v1/recovery/operations/{operation["id"]}/reconciliation', headers=auditor)
                assert response.status_code == 200
                receipt = response.json()
                assert receipt['status'] == 'verified' and not receipt['replay_permitted']
                assert len(receipt['expected']['lines']) == 2
                blocked = client.post(f'/api/v1/operations/{operation["id"]}/process', headers=buyer)
                assert blocked.json()['error']['code'] == 'RECOVERY_OPERATION_HELD'
                assert erp.count() == 1
                report.update(status='passed', requested_lines=2, complete_suppliers=2, selected_total='233.00',
                    quote_freight_count=1, source_line_evidence=True, partial_coverage_blocked=True,
                    independent_readback='verified', lost_response_reconciled=True, mock_drafts=1,
                    recovery_hold_replay_blocked=True)
            return 0
        except Exception as error:
            report['reason'] = type(error).__name__
            return 1
        finally:
            app.state.service.db.engine.dispose()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(run(parser.parse_args().output.resolve()))
