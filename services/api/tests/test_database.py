"""Offline/unit DB contracts. These are NOT live PostgreSQL tests."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from procureflow.app import create_app
from procureflow.config import Settings
from procureflow.database_config import normalize_database_url
from procureflow.db import Database, Base, OperationRow, RequestRow, QuoteVersionRow, audit
from procureflow.contracts import Principal
from procureflow.worker import pending_work_query
from conftest import approved, enqueue, BUYER


@pytest.mark.parametrize("prefix", ["postgres", "postgresql", "postgresql+psycopg"])
def test_postgres_alias_selects_sync_psycopg(prefix):
    assert normalize_database_url(prefix + "://u:p%40ss@localhost/example_test?sslmode=require") == (
        "postgresql+psycopg://u:p%40ss@localhost/example_test?sslmode=require")


@pytest.mark.parametrize("url", ["mysql://u:secret@host/db", "postgresql+asyncpg://u:secret@host/db",
    "sqlite+aiosqlite:///db", "secret-not-a-url", "postgresql://u:secret@host"])
def test_invalid_database_config_is_sanitized(url):
    with pytest.raises(ValueError) as error:
        normalize_database_url(url)
    assert "secret" not in str(error.value)


def test_settings_repr_hides_secrets(tmp_path):
    s = Settings(data_dir=tmp_path, database_url="postgresql://u:password-123@localhost/test",
                 erp_api_key="key-123", erp_api_secret="secret-123")
    assert all(value not in repr(s) for value in ("password-123", "key-123", "secret-123", "demo-buyer"))


def test_readiness_is_read_only_and_requires_migration_for_non_demo(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/missing.sqlite3")
    with pytest.raises(RuntimeError, match="SCHEMA_MISSING"):
        db.check_ready()
    Base.metadata.create_all(db.engine)
    assert db.check_ready(require_migrations=False)["status"] == "ok"
    with pytest.raises(RuntimeError, match="MIGRATION_REQUIRED"):
        db.check_ready()
    db.engine.dispose()


def test_readiness_redacts_database_error(system, monkeypatch):
    client, service, _ = system
    def unavailable(**kwargs):
        raise OperationalError("password-sensitive", None, RuntimeError("private-host"))
    monkeypatch.setattr(service.db, "check_ready", unavailable)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "error": "DATABASE_NOT_READY"}
    assert client.get("/health").status_code == 200  # Liveness differs from readiness.


def test_transaction_exception_rolls_back(system):
    _, service, _ = system
    with pytest.raises(RuntimeError):
        with service.db.transaction(write=True) as session:
            session.add(RequestRow(id="rollback-check", tenant_id="demo", owner_id="buyer",
                                   data={}, version=1, status="DRAFT"))
            session.flush()
            raise RuntimeError("abort")
    with service.db.transaction() as session:
        assert session.get(RequestRow, "rollback-check") is None


def test_pending_query_filters_live_leases(system):
    client, service, _ = system
    r, _, p = approved(client)
    operation = enqueue(client, r, p)
    with service.db.transaction(write=True) as session:
        row = session.get(OperationRow, operation["id"])
        row.status = "IN_FLIGHT"
        row.lease_until = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    with service.db.transaction() as session:
        assert list(session.execute(pending_work_query())) == []
    with service.db.transaction(write=True) as session:
        row = session.get(OperationRow, operation["id"])
        row.lease_until = "2000-01-01T00:00:00+00:00"
    with service.db.transaction() as session:
        assert session.execute(pending_work_query()).first()[0] == operation["id"]


def test_quote_refresh_replaces_stale_identity_map(system):
    client, service, _ = system
    r, q, _ = approved(client)
    principal = Principal(user_id="buyer-01", tenant_id="demo", role="buyer")
    with service.db.transaction() as session:
        _, cached = service._quote(session, principal, q["id"])
        cached.confirmed_by = "stale-cache"
        # no_autoflush simulates a stale ORM object without persisting it.
        with session.no_autoflush:
            _, current = service._quote(session, principal, q["id"], refresh=True)
        assert current.confirmed_by == "buyer-01"
