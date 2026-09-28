"""Real PostgreSQL gates. No SQLite fallback, no ERPNext/model credentials."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from procureflow.app import create_app
from procureflow.config import Settings
from procureflow.contracts import Principal, QuoteConfirm, RequestUpdate
from procureflow.db import Database, EventRow, OperationRow, OutboxRow, RequestRow
from procureflow.erp import MockERP
from procureflow.errors import DomainError
from conftest import approved, enqueue, ready, request, upload, BUYER, OTHER

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[3]
BUYER_ID = Principal(user_id="buyer-01", tenant_id="demo", role="buyer")


@pytest.fixture
def pg_system(pg_database, tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=pg_database.engine.url.render_as_string(hide_password=False))
    erp = MockERP(tmp_path / "mock-erp.sqlite3")
    with TestClient(create_app(settings, database=pg_database, erp=erp)) as client:
        yield client, client.app.state.service, erp


def test_postgres_migration_roundtrip(pg_database):
    cfg = Config(str(ROOT / "services/api/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "services/api/alembic"))
    with pg_database.engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "base")
    with pytest.raises(RuntimeError, match="SCHEMA_MISSING"):
        pg_database.check_ready()
    with pg_database.engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    assert pg_database.check_ready()["schema"] == "current"


def test_postgres_independent_worker_and_api_restart(pg_system):
    client, service, erp = pg_system
    assert client.get("/ready").json()["database"] == "postgresql"
    r, _, p = approved(client)
    op = enqueue(client, r, p)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "services/api"), "PF_MODE": "demo", "PF_ERP_MODE": "mock",
           "PF_DATABASE_URL": service.settings.database_url, "PF_DATA_DIR": str(service.settings.data_dir),
           "ERP_ALLOW_DRAFT_WRITES": "false"}
    for key in ("PF_AUTH_TOKENS", "ERP_API_KEY", "ERP_API_SECRET", "ERP_BASE_URL", "LLM_API_KEY"):
        env.pop(key, None)
    result = subprocess.run([sys.executable, "-m", "procureflow.worker", "--once"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30)
    # Avoid printing a DSN from driver tracebacks on a failing developer machine.
    assert result.returncode == 0, "Independent PostgreSQL worker failed; inspect local private logs"
    new_db = Database(service.settings.database_url)
    with TestClient(create_app(service.settings, database=new_db, erp=erp)) as restarted:
        current = restarted.get(f"/api/v1/operations/{op['id']}", headers=BUYER).json()
        assert current["status"] == "COMPLETED"
        assert enqueue(restarted, r, p)["id"] == op["id"]
        assert restarted.get(f"/api/v1/requests/{r['id']}", headers=OTHER).status_code == 404
    assert erp.count() == 1


def test_postgres_concurrent_reservation_is_one_operation(pg_system):
    client, service, _ = pg_system
    r, _, p = approved(client)
    barrier = threading.Barrier(6)
    def reserve(_):
        barrier.wait(timeout=10)
        return service.enqueue(BUYER_ID, r["id"], p["snapshot_hash"])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(6)))
    assert len({op["id"] for op in results}) == 1
    with service.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OperationRow)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxRow)) == 1


def test_postgres_concurrent_dispatch_calls_create_once(pg_system, monkeypatch):
    client, service, erp = pg_system
    r, _, p = approved(client)
    op = enqueue(client, r, p)
    create = erp.create_draft
    calls, mutex, barrier = [], threading.Lock(), threading.Barrier(6)
    def counted(key, payload):
        with mutex:
            calls.append(key)
        return create(key, payload)
    monkeypatch.setattr(erp, "create_draft", counted)
    def dispatch(_):
        barrier.wait(timeout=10)
        return service.process_operation(BUYER_ID, op["id"])
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(dispatch, range(6)))
    assert service.get_operation(BUYER_ID, op["id"])["status"] == "COMPLETED"
    assert calls == [op["id"]]  # Remote INSERT OR IGNORE must not mask duplicate calls.
    assert erp.count() == 1


def test_postgres_concurrent_edits_detect_version_conflict(pg_system):
    client, service, _ = pg_system
    r, _, _ = approved(client)
    current = service.get_request(BUYER_ID, r["id"])
    barrier = threading.Barrier(2)
    def edit(quantity):
        cmd = RequestUpdate(title=current["title"], sku=current["sku"], quantity=quantity,
            budget=current["budget"], max_delivery_days=current["max_delivery_days"],
            expected_version=current["version"])
        barrier.wait(timeout=10)
        try:
            service.update_request(BUYER_ID, r["id"], cmd)
            return "UPDATED"
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["21", "22"]))
    assert sorted(results) == ["UPDATED", "VERSION_CONFLICT"]
    assert service.get_request(BUYER_ID, r["id"])["status"] == "APPROVAL_STALE"


def test_postgres_waiting_confirmation_refreshes_quote_version(pg_system, monkeypatch):
    client, service, _ = pg_system
    r = request(client)
    q = upload(client, r["id"])
    before = service.get_request(BUYER_ID, r["id"])["version"]
    has_read, release = threading.Event(), threading.Event()
    original = service._request
    def delayed(session, principal, request_id, lock=False):
        if threading.current_thread().name.startswith("stale-confirm") and lock:
            has_read.set()
            assert release.wait(10)
        return original(session, principal, request_id, lock=lock)
    monkeypatch.setattr(service, "_request", delayed)
    cmd = QuoteConfirm(expected_version=1, acknowledge=True)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-confirm") as pool:
        second = pool.submit(service.confirm_quote, BUYER_ID, q["id"], cmd)
        try:
            assert has_read.wait(10)
            service.confirm_quote(BUYER_ID, q["id"], cmd)
        finally:
            release.set()
        assert second.result(timeout=15)["confirmed_by"] == "buyer-01"
    assert service.get_request(BUYER_ID, r["id"])["version"] == before + 1
    with service.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(EventRow).where(EventRow.type == "QUOTE_CONFIRMED")) == 1


def test_postgres_lost_response_only_reconciles(pg_system):
    client, service, erp = pg_system
    r, _, p = approved(client)
    op = enqueue(client, r, p)
    erp.fail_after_commit_once.add(op["id"])
    assert service.process_operation(BUYER_ID, op["id"])["status"] == "RECONCILING"
    assert service.process_operation(BUYER_ID, op["id"])["status"] == "COMPLETED"
    assert erp.count() == 1


def test_postgres_uniqueness_is_enforced_by_database(pg_system):
    client, service, _ = pg_system
    r, _, p = approved(client)
    op = enqueue(client, r, p)
    with pytest.raises(IntegrityError):
        with service.db.transaction(write=True) as session:
            row = session.get(OperationRow, op["id"])
            session.add(OperationRow(id="duplicate", tenant_id=row.tenant_id, request_id=row.request_id,
                approval_id=row.approval_id, snapshot_hash=row.snapshot_hash, payload=row.payload, status="PENDING"))
            session.flush()
    with pytest.raises(IntegrityError):
        with service.db.transaction(write=True) as session:
            session.add(OutboxRow(id="duplicate-outbox", operation_id=op["id"], status="PENDING"))
            session.flush()


def test_postgres_rollback_keeps_operation_and_outbox_atomic(pg_system, monkeypatch):
    client, service, _ = pg_system
    r, _, p = approved(client)
    import procureflow.service as module
    original = module.audit
    def fail_reservation(session, principal, rid, kind, payload):
        if kind == "ERP_OPERATION_RESERVED":
            session.flush()  # operation + outbox exist inside the uncommitted transaction
            raise RuntimeError("TEST_ROLLBACK")
        return original(session, principal, rid, kind, payload)
    monkeypatch.setattr(module, "audit", fail_reservation)
    with pytest.raises(RuntimeError, match="TEST_ROLLBACK"):
        service.enqueue(BUYER_ID, r["id"], p["snapshot_hash"])
    with service.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OperationRow)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxRow)) == 0
        assert session.get(RequestRow, r["id"]).status == "APPROVED"
