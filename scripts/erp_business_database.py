"""Isolated PostgreSQL business storage for the disposable real-ERP acceptance gate.

Never reads PF_DATABASE_URL, changes database roles, or drops an existing schema.
Only this process's randomly named schema is removed, including on failure.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from procureflow.database_config import normalize_database_url


def postgres_test_url(environ=None):
    env = os.environ if environ is None else environ
    try:
        # libpq can route elsewhere via PGHOSTADDR/PGSERVICE even when the URL
        # names loopback. This lab accepts no ambient libpq configuration.
        if env.get('PF_ALLOW_DATABASE_TESTS') != '1' or any(key.startswith('PG') for key in env):
            raise ValueError
        parsed = make_url(normalize_database_url(env.get('PF_TEST_DATABASE_URL', '')))
        if (parsed.drivername != 'postgresql+psycopg'
                or parsed.host not in {'127.0.0.1', '::1'}
                or not re.fullmatch(r'[A-Za-z0-9_]+_test', parsed.database or '')
                or parsed.query):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        # URLs, driver exceptions and environment values must not enter reports.
        raise ValueError('POSTGRES_LAB_CONFIGURATION_REQUIRED') from None


@contextmanager
def business_database(backend: str, directory: str):
    if backend == 'sqlite':
        yield f'sqlite:///{Path(directory) / "business.sqlite3"}'
        return
    if backend != 'postgresql':
        raise ValueError('UNSUPPORTED_BUSINESS_DATABASE')
    parsed = postgres_test_url()
    schema = 'pf_erp_test_' + secrets.token_hex(16)
    engine = create_engine(parsed, hide_parameters=True, connect_args={'connect_timeout': 10})
    created = False
    try:
        with engine.begin() as connection:
            if connection.scalar(text('SELECT current_database()')) != parsed.database:
                raise ValueError('POSTGRES_LAB_TARGET_MISMATCH')
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        isolated = parsed.update_query_dict({'options': f'-csearch_path={schema}'})
        yield isolated.render_as_string(hide_password=False)
    finally:
        try:
            if created:
                with engine.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            engine.dispose()


def require(condition):
    if not condition:
        raise ValueError('POSTGRES_BUSINESS_AUDIT_FAILED')


def audit_business_database(url: str, operations: list[dict]) -> dict:
    """Independent SQL read after API restart, before our schema is removed."""
    parsed = make_url(url)
    options = parsed.query.get('options', '')
    if (parsed.drivername != 'postgresql+psycopg'
            or re.fullmatch(r'-csearch_path=pf_erp_test_[0-9a-f]{32}', options) is None):
        raise ValueError('POSTGRES_LAB_SCHEMA_REQUIRED')
    schema = options.split('=', 1)[1]
    engine = create_engine(parsed, hide_parameters=True, connect_args={'connect_timeout': 10})
    try:
        with engine.connect() as connection:
            require(connection.scalar(text('SELECT current_schema()')) == schema)
            from alembic.script import ScriptDirectory
            heads = set(ScriptDirectory(str(ROOT / 'services/api/alembic')).get_heads())
            require(set(connection.scalars(text('SELECT version_num FROM alembic_version'))) == heads)
            rows = connection.execute(text(
                'SELECT id, tenant_id, status, snapshot_hash, remote_id FROM external_operations'
            )).mappings().all()
            require(len(rows) == len(operations) and len(operations) > 0)
            for expected in operations:
                row, = [row for row in rows if row['id'] == expected['operation_id']]
                require(row['tenant_id'] == 'lab' and row['status'] == 'COMPLETED')
                require(row['snapshot_hash'] == expected['snapshot_hash'] and row['remote_id'] == expected['remote_id'])
            outbox = connection.execute(text('SELECT operation_id, status FROM outbox')).mappings().all()
            require(len(outbox) == len(operations) and {row['operation_id'] for row in outbox} == {row['id'] for row in rows})
            require(all(row['status'] == 'DONE' for row in outbox))
            receipts = connection.execute(text(
                "SELECT payload FROM audit_events WHERE type = 'ERP_VERIFICATION_VERIFIED'"
            )).scalars().all()
            require(len(receipts) == len(operations))
            require({receipt['operation_id'] for receipt in receipts} == {row['id'] for row in rows})
            for receipt in receipts:
                expected, = [op for op in operations if op['operation_id'] == receipt['operation_id']]
                require(receipt['remote_id'] == expected['remote_id'] and receipt['snapshot_hash'] == expected['snapshot_hash'])
                require(receipt['simulated'] is False and receipt['external_write_attempted'] is False)
            version = connection.scalar(text("SELECT current_setting('server_version_num')"))
            require(isinstance(version, str) and version.isdigit())
            return {'status': 'passed', 'database': 'PostgreSQL', 'server_version_num': version,
                'isolated_schema': True, 'migration_current': True, 'operation_count': len(operations),
                'completed_operation_count': len(operations), 'outbox_count': len(operations), 'done_outbox_count': len(operations),
                'verified_receipt_count': len(operations), 'operation_identity_matches': True,
                'read_only_audit': True, 'after_api_restart': True}
    finally:
        engine.dispose()
