"""Regenerate committed API contracts without touching the user's data store."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
with tempfile.TemporaryDirectory(prefix='pf-contracts-') as directory:
    os.environ['PF_DATA_DIR'] = directory
    os.environ['PF_DATABASE_URL'] = f'sqlite:///{directory}/contracts.sqlite3'
    os.environ['PF_MODE'] = 'demo'
    os.environ['PF_ERP_MODE'] = 'mock'
    os.environ['ERP_ALLOW_DRAFT_WRITES'] = 'false'
    os.environ.pop('PF_AUTH_TOKENS', None)
    from procureflow.app import app
    from procureflow.contracts import QuoteValues, RequestCreate
    out = ROOT / 'packages/contracts'
    out.mkdir(parents=True, exist_ok=True)
    for name, schema in [('openapi.json',app.openapi()),('quotation.schema.json',QuoteValues.model_json_schema()),
                         ('request.schema.json',RequestCreate.model_json_schema())]:
        (out/name).write_text(json.dumps(schema, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    app.state.service.db.engine.dispose()
print('Contracts exported to packages/contracts/')
