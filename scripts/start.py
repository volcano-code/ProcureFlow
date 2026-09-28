"""Start the local-only workbench; run from the repository root."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8000)
parser.add_argument('--data-dir', type=Path, default=ROOT / '.data')
args = parser.parse_args()
os.environ.setdefault('PF_DATA_DIR', str(args.data_dir.resolve()))
sys.path.insert(0, str(ROOT / 'services' / 'api'))
try:
    import uvicorn
except ImportError:
    raise SystemExit('Install dependencies: python -m pip install -r services/api/requirements.txt')
if not 1024 <= args.port <= 65535:
    raise SystemExit('Use an unprivileged port between 1024 and 65535')
print(f'ProcureFlow local alpha: http://127.0.0.1:{args.port} (mock ERP by default)', flush=True)
uvicorn.run('procureflow.app:app', host='127.0.0.1', port=args.port)
