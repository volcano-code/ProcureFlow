from __future__ import annotations
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from procureflow.app import create_app
from procureflow.config import Settings
from procureflow.db import Database
from procureflow.erp import MockERP

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "evals" / "fixtures"
BUYER = {"Authorization": "Bearer demo-buyer"}
APPROVER = {"Authorization": "Bearer demo-approver"}
AUDITOR = {"Authorization": "Bearer demo-auditor"}
OTHER = {"Authorization": "Bearer demo-other-tenant"}


@pytest.fixture
def system(tmp_path, request):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}")
    if __import__("os").getenv("PF_TEST_BACKEND") == "postgresql":
        db = request.getfixturevalue("pg_database")
        settings = Settings(data_dir=tmp_path, database_url=db.engine.url.render_as_string(hide_password=False))
    else:
        db = Database(settings.database_url, create_schema=True)
    erp = MockERP(tmp_path / "remote.sqlite3")
    app = create_app(settings=settings, database=db, erp=erp)
    with TestClient(app) as client:
        yield client, app.state.service, erp


def request(client):
    response = client.post("/api/v1/requests", headers=BUYER, json={"title":"Test procurement","sku":"STAND-01",
        "quantity":"20","budget":"30000.00","max_delivery_days":14})
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, rid, filename="supplier-a.txt", content=None):
    content = (FIXTURES / filename).read_bytes() if content is None else content
    response = client.post(f"/api/v1/requests/{rid}/documents", headers=BUYER, files={"file": (filename, content)})
    assert response.status_code == 201, response.text
    return response.json()


def ready(client):
    r = request(client)
    q = upload(client, r["id"])
    response = client.post(f"/api/v1/quotes/{q['id']}/confirm", headers=BUYER,
                          json={"expected_version": 1, "acknowledge": True})
    assert response.status_code == 200, response.text
    response = client.post(f"/api/v1/requests/{r['id']}/analyze", headers=BUYER, json={})
    assert response.status_code == 200, response.text
    p = response.json()["proposal"]
    assert p
    return r, q, p


def approved(client):
    r,q,p = ready(client)
    response = client.post(f"/api/v1/requests/{r['id']}/approval", headers=APPROVER,
                          json={"snapshot_hash":p["snapshot_hash"]})
    assert response.status_code == 200, response.text
    return r,q,p


def enqueue(client, r, p):
    response = client.post(f"/api/v1/requests/{r['id']}/execute", headers=BUYER,
                          json={"snapshot_hash":p["snapshot_hash"]})
    assert response.status_code == 202, response.text
    return response.json()


@pytest.fixture
def pg_database(tmp_path):
    """An explicit disposable PostgreSQL schema; never use PF_DATABASE_URL.

    Refuse arbitrary databases and drop ONLY this fixture's random schema.
    The CI runner creates a dedicated *_test database before invoking this.
    """
    import os
    from uuid import uuid4
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from alembic.config import Config
    from alembic import command
    from procureflow.database_config import normalize_database_url

    url = os.getenv("PF_TEST_DATABASE_URL", "")
    if not url or os.getenv("PF_ALLOW_DATABASE_TESTS") != "1":
        if os.getenv("PF_REQUIRE_POSTGRES") == "1":
            pytest.fail("POSTGRES_TEST_CONFIGURATION_REQUIRED")
        pytest.skip("PostgreSQL tests require a dedicated *_test DB and explicit opt-in")
    parsed = make_url(normalize_database_url(url))
    if parsed.get_backend_name() != "postgresql" or not parsed.database.endswith("_test"):
        pytest.fail("REFUSE_NON_TEST_DATABASE")
    if __import__("importlib.util", fromlist=["find_spec"]).find_spec("psycopg") is None:
        pytest.fail("Install requirements-postgres.txt; no fallback to SQLite")
    schema = "pf_test_" + uuid4().hex
    admin = create_engine(parsed, hide_parameters=True, connect_args={"connect_timeout": 10})
    db = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        # Override options instead of inheriting an ambient search_path.
        isolated = parsed.update_query_dict({"options": f"-csearch_path={schema}"})
        db = Database(isolated.render_as_string(hide_password=False))
        cfg = Config(str(ROOT / "services/api/alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "services/api/alembic"))
        with db.engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        assert db.check_ready()["database"] == "postgresql"
        yield db
    finally:
        if db is not None:
            db.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def publish_policy(client, **overrides):
    current = client.get("/api/v1/policy", headers=APPROVER).json()
    body = {"expected_version": current["latest_version"], "budget_cap": current["budget_cap"],
            "max_delivery_days": current["max_delivery_days"], "minimum_valid_quotes": current["minimum_valid_quotes"],
            "reason": "Test tenant policy revision", **overrides}
    response = client.post("/api/v1/policy/versions", headers=APPROVER, json=body)
    assert response.status_code == 201, response.text
    return response.json()
