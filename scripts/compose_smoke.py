"""Loopback-only HTTP acceptance of the explicit PostgreSQL/demo/mock stack.

Creates synthetic requests/documents in the selected LOCAL demo stack. Never
accepts real ERP mode, private mode, arbitrary URLs or a non-PostgreSQL backend.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlparse
import httpx

ROOT = Path(__file__).resolve().parents[1]


def run(base_url: str, api_prefix: str = "") -> dict:
    if api_prefix not in {"", "/backend"}:
        raise ValueError("Only direct API or the fixed Next backend prefix is allowed")
    url = urlparse(base_url)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
        raise ValueError("Only an explicit loopback HTTP demo target is permitted")
    with httpx.Client(base_url=base_url, timeout=10, follow_redirects=False) as client:
        health = client.get(api_prefix + "/health")
        health.raise_for_status()
        assert health.json()["mode"] == "demo" and health.json()["erp"] == "mock"
        ready = client.get(api_prefix + "/ready")
        assert ready.status_code == 200 and ready.json()["database"] == "postgresql"
        def call(method, path, role="buyer", expected=200, **kwargs):
            response = client.request(method, api_prefix + "/api/v1" + path,
                headers={"Authorization": "Bearer demo-" + role}, **kwargs)
            assert response.status_code == expected, (path, response.status_code)
            return response.json()
        request = call("POST", "/requests", expected=201, json={"title": "Compose synthetic acceptance", "sku": "STAND-01",
            "quantity": "20", "budget": "30000.00", "max_delivery_days": 14})
        rid = request["id"]
        for name in ("supplier-a.txt", "supplier-b.csv", "supplier-c.pdf"):
            quote = call("POST", f"/requests/{rid}/documents", expected=201,
                         files={"file": (name, (ROOT / "evals/fixtures" / name).read_bytes())})
            call("POST", f"/quotes/{quote['id']}/confirm", json={"expected_version": 1, "acknowledge": True})
        proposal = call("POST", f"/requests/{rid}/analyze", json={})["proposal"]
        assert proposal["total"] == "24200.00" and proposal["quote_values"]["supplier_id"] == "SUP-C"
        call("POST", f"/requests/{rid}/approval", role="approver", json={"snapshot_hash": proposal["snapshot_hash"]})
        op = call("POST", f"/requests/{rid}/execute", expected=202, json={"snapshot_hash": proposal["snapshot_hash"]})
        for _ in range(40):
            current = call("GET", f"/operations/{op['id']}")
            if current["status"] == "COMPLETED":
                break
            assert current["status"] not in {"NEEDS_HUMAN", "RECONCILING"}, current["status"]
            time.sleep(.5)
        else:
            raise AssertionError("Independent Compose worker did not complete the operation")
        replay = call("POST", f"/requests/{rid}/execute", expected=202, json={"snapshot_hash": proposal["snapshot_hash"]})
        assert replay["id"] == op["id"] and replay["remote_id"] == current["remote_id"]
        return {"status": "passed", "database": "postgresql", "erp": "mock", "live_erp": False,
                "live_model": False, "request_id": rid, "operation_id": op["id"], "remote_id": current["remote_id"],
                "total": proposal["total"], "worker_completed": True, "replay_same_operation": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-prefix", choices=("", "/backend"), default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    code = 0
    try:
        report = run(args.base_url, args.api_prefix)
    except (AssertionError, ValueError, httpx.HTTPError, KeyError):
        code = 1
        report = {"status": "failed", "reason": "COMPOSE_SMOKE_FAILED", "live_erp": False, "live_model": False}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(code)
