"""Create random credentials for a NEW disposable ERPNext lab; never overwrite."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import secrets


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--create-ephemeral', action='store_true')
    p.add_argument('--output', type=Path, default=Path('.env.erp-sandbox'))
    a = p.parse_args(argv)
    if not a.create_ephemeral:
        print('BLOCKED: --create-ephemeral is required; no file created')
        return 2
    content = '\n'.join(f'{key}={secrets.token_hex(32)}' for key in
        ('PF_EPHEMERAL_NONCE','ERP_LAB_DB_PASSWORD','ERP_LAB_ADMIN_PASSWORD')) + '\n'
    try:
        fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd,'w') as f: f.write(content)
    except FileExistsError:
        print('BLOCKED: sandbox credential file already exists; not overwritten')
        return 2
    print('Created private local lab credentials. Do not commit or share this file.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
