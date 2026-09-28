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
def system(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}")
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
