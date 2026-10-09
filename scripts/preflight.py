"""Offline environment report; optional explicit GET-only ERPNext configuration probe.

Never creates/submits/deletes business documents and never prints credentials.
Live access is attempted ONLY with --erp-read-only, private identity configuration,
and a configured ERPNext test endpoint. The default is entirely offline.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))


def environment_report() -> dict:
    return {
        'scope': 'offline environment inventory, not integration acceptance',
        'status': 'inventory_only',
        'python': sys.version.split()[0],
        'executables': {name: shutil.which(name) is not None for name in ('node', 'npm', 'docker', 'psql')},
        'python_packages': {name: importlib.util.find_spec(name) is not None
                            for name in ('fastapi', 'sqlalchemy', 'httpx', 'playwright', 'psycopg', 'langgraph', 'mcp')},
        'next_dependency_installed': (ROOT / 'apps/web/node_modules/next/dist/bin/next').is_file(),
        'npm_lockfile_present': (ROOT / 'apps/web/package-lock.json').is_file(),
        'erp_configuration_present': {key: bool(os.getenv(key))
                                      for key in ('ERP_BASE_URL', 'ERP_API_KEY', 'ERP_API_SECRET', 'ERP_COMPANY', 'PF_AUTH_TOKENS')},
        'model_configuration_present': {key: bool(os.getenv(key)) for key in ('LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL')},
        'erp_probe_attempted': False,
        'external_writes_attempted': 0,
        'native_browser_verified': False,
        'live_draft_roundtrip_verified': False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--erp-read-only', action='store_true', help='Explicitly probe the configured test ERPNext with GET only')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    report = environment_report()
    exit_code = 0
    if args.erp_read_only:
        if (os.getenv('PF_MODE') != 'private' or os.getenv('PF_ERP_MODE') != 'erpnext'
                or not all(report['erp_configuration_present'].values())):
            report.update(status='blocked', reason='PRIVATE_ERPNEXT_CONFIGURATION_REQUIRED')
            exit_code = 2
        else:
            from procureflow.config import Settings
            from procureflow.erp import ERPNextClient, ERPRejected, ERPUnknown
            try:
                # Validate configured identities without touching an application database.
                with tempfile.TemporaryDirectory(prefix='pf-preflight-') as tmp:
                    settings = Settings(data_dir=Path(tmp), database_url='sqlite:///:memory:')
                    client = ERPNextClient(settings.erp_url, settings.erp_api_key, settings.erp_api_secret,
                                           settings.erp_company, allow_writes=False,
                                           tax_account=settings.erp_tax_account, freight_account=settings.erp_freight_account)
                    try:
                        report['erp_probe_attempted'] = True
                        report['erp'] = client.preflight()
                        report['status'] = 'read_only_checks_passed'
                    finally:
                        client.client.close()
            except (ERPRejected, ERPUnknown, ValueError, TypeError, AttributeError) as error:
                # Configuration exceptions and transport failures must not echo secrets/URLs.
                code = str(error) if isinstance(error, (ERPRejected, ERPUnknown)) else 'INVALID_PRIVATE_CONFIGURATION'
                report.update(status='failed', reason=code if re.fullmatch(r'[A-Z0-9_]+', code) else 'ERP_PROBE_FAILED')
                exit_code = 1
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
