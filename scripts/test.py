"""Run tests in a subprocess with a disposable data directory."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='procureflow-test-') as path:
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'services/api'), 'PF_DATA_DIR': path, 'PF_MODE': 'demo', 'PF_ERP_MODE': 'mock'}
    env.update(PF_MODE='demo', PF_ERP_MODE='mock', ERP_ALLOW_DRAFT_WRITES='false')
    for key in ('ERP_API_KEY', 'ERP_API_SECRET', 'ERP_BASE_URL', 'ERP_COMPANY', 'LLM_API_KEY'):
        env.pop(key, None)
    env.pop('PF_AUTH_TOKENS', None)
    env.pop('PF_DATABASE_URL', None)
    result = subprocess.run([sys.executable, '-m', 'pytest', *(sys.argv[1:] or ['-q'])], cwd=ROOT / 'services/api', env=env)
    raise SystemExit(result.returncode)
