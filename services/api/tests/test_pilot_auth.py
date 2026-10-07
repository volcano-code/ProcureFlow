"""Synthetic local identity tests; no real accounts, provisioning or deployment."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from procureflow.app import create_app
from procureflow.auth import IdentityService, secret_hash
from procureflow.config import Settings
from procureflow.contracts import LoginCommand, Principal
from procureflow.db import (Database, EventRow, PilotInviteRow, PilotMembershipRow, PilotSessionRow,
                            PilotTenantRow)
from procureflow.errors import DomainError

ROOT = Path(__file__).resolve().parents[3]
PAST = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def identity_system(tmp_path, request):
    if os.getenv("PF_TEST_BACKEND") == "postgresql":
        db = request.getfixturevalue("pg_database")
        settings = Settings(data_dir=tmp_path, mode="pilot", auth_tokens={},
            database_url=db.engine.url.render_as_string(hide_password=False))
    else:
        settings = Settings(data_dir=tmp_path, mode="pilot", auth_tokens={},
            database_url=f"sqlite:///{tmp_path / 'pilot.sqlite3'}")
        db = Database(settings.database_url, create_schema=True)
    identities = IdentityService(db, settings)
    identities.create_tenant("synthetic-pilot")
    identities.set_membership("synthetic-pilot", "buyer", "buyer")
    identities.set_membership("synthetic-pilot", "approver", "approver")
    with TestClient(create_app(settings, database=db)) as client:
        yield client, identities, db, settings


def login(client, identities, user="buyer"):
    invite = identities.issue_invite("synthetic-pilot", user)
    response = client.post("/api/v1/auth/login", json={"credential": invite["credential"]})
    assert response.status_code == 200, response.text
    return response.json(), invite


def bearer(result):
    return {"Authorization": "Bearer " + result["token"]}


def assert_denied(identities, token):
    with pytest.raises(DomainError) as caught:
        identities.authenticate(token)
    assert caught.value.status_code == 401
    assert token not in str(caught.value)


def test_pilot_settings_fail_closed_and_bound_session_ttl(tmp_path, monkeypatch):
    monkeypatch.delenv("PF_AUTH_TOKENS", raising=False)
    assert Settings(data_dir=tmp_path, mode="pilot").auth_tokens == {}
    with pytest.raises(ValueError, match="forbids"):
        Settings(data_dir=tmp_path, mode="pilot", auth_tokens={"x" * 40: {
            "tenant_id": "t", "user_id": "u", "role": "buyer"}})
    for ttl in (0, 59, 3601):
        with pytest.raises(ValueError, match="SESSION_TTL"):
            Settings(data_dir=tmp_path, mode="pilot", session_ttl_seconds=ttl)


def test_login_is_hash_only_one_use_and_public_principal_is_minimal(identity_system, caplog):
    client, identities, db, _ = identity_system
    result, invite = login(client, identities)
    assert result["token_type"] == "bearer"
    principal = identities.authenticate(result["token"])
    assert result["identity"] == {"user_id": "buyer", "tenant_id": "synthetic-pilot", "role": "buyer"}
    assert principal.session_id and principal.auth_version == "1:1"
    assert principal.model_dump() == result["identity"]
    assert "sess_" not in principal.model_dump_json() and "1:1" not in repr(principal)
    me = client.get("/api/v1/me", headers=bearer(result))
    assert me.json() == {**result["identity"], "expires_at": result["expires_at"]}
    assert me.headers["cache-control"] == "no-store"
    assert "set-cookie" not in me.headers
    assert client.post("/api/v1/auth/login", json={"credential": invite["credential"]}).status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer " + invite["credential"]}).status_code == 401
    with db.transaction() as session:
        stored_invite = session.get(PilotInviteRow, invite["invite_id"])
        stored_session = session.get(PilotSessionRow, principal.session_id)
        assert stored_invite.credential_hash == secret_hash(invite["credential"])
        assert stored_session.token_hash == secret_hash(result["token"])
        assert stored_invite.consumed_at is not None
        for name in ("pilot_invites", "pilot_sessions", "audit_events"):
            content = repr(session.execute(text(f"SELECT * FROM {name}")).all())
            assert invite["credential"] not in content and result["token"] not in content
    assert invite["credential"] not in caplog.text and result["token"] not in caplog.text
    assert invite["credential"] not in repr(LoginCommand(credential=invite["credential"]))


@pytest.mark.parametrize("body", [
    {"credential": "pfi_" + "SENSITIVE" * 100},
    {"credential": "SENSITIVE"},
    {"credential": {"SENSITIVE": "SENSITIVE"}},
    {"credential": "pfi_" + "SENSITIVE" * 5, "password": "SENSITIVE"},
    ["SENSITIVE"],
])
def test_login_validation_never_echoes_credentials(identity_system, body):
    client, _, _, _ = identity_system
    response = client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 422
    assert "SENSITIVE" not in response.text
    assert response.json()["error"]["code"] == "INVALID_LOGIN"


def test_malformed_json_and_invalid_credentials_are_redacted(identity_system):
    client, _, _, _ = identity_system
    for value in ('{"credential":"SENSITIVE"', '"SENSITIVE"'):
        response = client.post("/api/v1/auth/login", content=value, headers={"Content-Type": "application/json"})
        assert response.status_code == 422 and "SENSITIVE" not in response.text
    response = client.post("/api/v1/auth/login", json={"credential": "pfi_" + "SENSITIVE" * 5})
    assert response.status_code == 401 and "SENSITIVE" not in response.text
    assert client.get("/api/v1/auth/config").json() == {
        "mode": "pilot", "login_method": "invite", "session_ttl_seconds": 1800}
    for token in ("demo-buyer", "wrong" * 10, "pfs_" + "x" * 500):
        assert client.get("/api/v1/me", headers={"Authorization": "Bearer " + token}).status_code == 401


@pytest.mark.parametrize("kind", ["invite-revoked", "invite-expired", "member-disabled", "member-expired",
                                 "tenant-disabled", "role-changed"])
def test_invalid_invites_never_create_sessions(identity_system, kind):
    client, identities, db, _ = identity_system
    invite = identities.issue_invite("synthetic-pilot", "buyer")
    if kind == "invite-revoked":
        identities.revoke_invite(invite["invite_id"])
    elif kind == "invite-expired":
        with db.transaction(write=True) as session:
            session.get(PilotInviteRow, invite["invite_id"]).expires_at = PAST
    elif kind == "member-disabled":
        identities.set_membership("synthetic-pilot", "buyer", "buyer", active=False)
    elif kind == "member-expired":
        identities.set_membership("synthetic-pilot", "buyer", "buyer", expires_at=PAST)
    elif kind == "tenant-disabled":
        identities.set_tenant_active("synthetic-pilot", False)
    else:
        identities.set_membership("synthetic-pilot", "buyer", "auditor")
    assert client.post("/api/v1/auth/login", json={"credential": invite["credential"]}).status_code == 401
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PilotSessionRow)) == 0


@pytest.mark.parametrize("kind", ["logout", "session-revoked", "session-expired", "all-sessions-revoked",
                                 "member-disabled", "member-expired", "tenant-disabled", "role-changed"])
def test_sessions_and_cached_principals_revalidate_live_authority(identity_system, kind):
    client, identities, db, _ = identity_system
    result, _ = login(client, identities)
    principal = identities.authenticate(result["token"])
    if kind == "logout":
        assert client.post("/api/v1/auth/logout", headers=bearer(result)).status_code == 200
    elif kind == "session-revoked":
        identities.revoke_session(principal.session_id)
    elif kind == "session-expired":
        with db.transaction(write=True) as session:
            session.get(PilotSessionRow, principal.session_id).expires_at = PAST
    elif kind == "all-sessions-revoked":
        identities.revoke_user_sessions("synthetic-pilot", "buyer")
    elif kind == "member-disabled":
        identities.set_membership("synthetic-pilot", "buyer", "buyer", active=False)
    elif kind == "member-expired":
        identities.set_membership("synthetic-pilot", "buyer", "buyer", expires_at=PAST)
    elif kind == "tenant-disabled":
        identities.set_tenant_active("synthetic-pilot", False)
    else:
        identities.set_membership("synthetic-pilot", "buyer", "auditor")
    assert_denied(identities, result["token"])
    assert client.get("/api/v1/me", headers=bearer(result)).status_code == 401
    with pytest.raises(DomainError):
        with db.transaction() as session:
            identities.validate_principal(session, principal)


def test_approval_generation_cannot_resurrect_on_reenable(identity_system):
    client, identities, db, _ = identity_system
    result, _ = login(client, identities, "approver")
    principal = identities.authenticate(result["token"])
    with db.transaction() as session:
        assert identities.approver_valid(session, "synthetic-pilot", "approver", principal.auth_version)
        assert not identities.approver_valid(session, "synthetic-pilot", "approver")
    for change in ("membership", "tenant", "role", "expiry"):
        if change == "membership":
            identities.set_membership("synthetic-pilot", "approver", "approver", active=False)
            identities.set_membership("synthetic-pilot", "approver", "approver", active=True)
        elif change == "tenant":
            identities.set_tenant_active("synthetic-pilot", False)
            identities.set_tenant_active("synthetic-pilot", True)
        elif change == "role":
            identities.set_membership("synthetic-pilot", "approver", "buyer")
            identities.set_membership("synthetic-pilot", "approver", "approver")
        else:
            identities.set_membership("synthetic-pilot", "approver", "approver", expires_at=PAST)
            identities.set_membership("synthetic-pilot", "approver", "approver", expires_at=None)
        with db.transaction() as session:
            assert not identities.approver_valid(session, "synthetic-pilot", "approver", principal.auth_version)
        assert_denied(identities, result["token"])
        result, _ = login(client, identities, "approver")
        principal = identities.authenticate(result["token"])


def test_natural_membership_expiry_invalidates_approvals_without_mutation(identity_system):
    client, identities, db, _ = identity_system
    result, _ = login(client, identities, "approver")
    principal = identities.authenticate(result["token"])
    with db.transaction(write=True) as session:
        # Simulate elapsed wall-clock time without incrementing the generation.
        member = session.get(PilotMembershipRow, ("synthetic-pilot", "approver"))
        member.expires_at = PAST
    with db.transaction() as session:
        assert not identities.approver_valid(session, "synthetic-pilot", "approver", principal.auth_version)
    assert_denied(identities, result["token"])


def test_restarts_keep_sessions_logout_and_revocation_durable(identity_system):
    client, identities, db, settings = identity_system
    result, _ = login(client, identities)
    new_db = Database(settings.database_url)
    try:
        restarted = IdentityService(new_db, settings)
        assert restarted.authenticate(result["token"]).user_id == "buyer"
        restarted.logout(restarted.authenticate(result["token"]))
        assert_denied(IdentityService(db, settings), result["token"])
        result, _ = login(client, identities)
        identities.set_membership("synthetic-pilot", "buyer", "buyer", active=False)
        assert_denied(restarted, result["token"])
    finally:
        new_db.engine.dispose()


def concurrent_exchange(identities, db):
    invite = identities.issue_invite("synthetic-pilot", "buyer")
    barrier = threading.Barrier(6)
    def consume(_):
        barrier.wait(timeout=10)
        try:
            return identities.login(invite["credential"])["token"]
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(consume, range(6)))
    assert results.count("UNAUTHENTICATED") == 5
    assert len([value for value in results if value.startswith("pfs_")]) == 1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PilotSessionRow)) == 1
        assert session.scalar(select(func.count()).select_from(EventRow).where(EventRow.type == "PILOT_SESSION_STARTED")) == 1


def test_concurrent_invite_exchange_has_exactly_one_winner(identity_system):
    _, identities, db, _ = identity_system
    concurrent_exchange(identities, db)


@pytest.mark.postgres
def test_postgres_concurrent_invite_exchange_has_exactly_one_winner(pg_database, tmp_path):
    settings = Settings(data_dir=tmp_path, mode="pilot", database_url=pg_database.engine.url.render_as_string(hide_password=False))
    identities = IdentityService(pg_database, settings)
    identities.create_tenant("synthetic-pilot")
    identities.set_membership("synthetic-pilot", "buyer", "buyer")
    concurrent_exchange(identities, pg_database)


def test_forged_principal_and_tenant_never_authorize(identity_system):
    client, identities, db, _ = identity_system
    result, _ = login(client, identities)
    principal = identities.authenticate(result["token"])
    forged = Principal(user_id="buyer", tenant_id="synthetic-pilot", role="buyer")
    for candidate in (forged, principal.model_copy(update={"role": "approver"}),
                      principal.model_copy(update={"user_id": "approver"}),
                      principal.model_copy(update={"tenant_id": "other"})):
        with pytest.raises(DomainError):
            with db.transaction() as session:
                identities.validate_principal(session, candidate)


def test_supplier_read_rechecks_session_before_return(identity_system, monkeypatch):
    client, identities, _, _ = identity_system
    result, _ = login(client, identities)
    def supplier_read():
        identities.revoke_user_sessions("synthetic-pilot", "buyer")
        return [{"id": "private-supplier"}]
    monkeypatch.setattr(client.app.state.service.erp, "suppliers", supplier_read)
    response = client.get("/api/v1/suppliers", headers=bearer(result))
    assert response.status_code == 401 and "private-supplier" not in response.text


def test_sse_invalid_session_is_terminal_and_has_no_later_data(identity_system, monkeypatch):
    client, identities, _, _ = identity_system
    result, _ = login(client, identities)
    response = client.post("/api/v1/requests", headers=bearer(result), json={
        "title": "Synthetic request", "sku": "STAND-01", "quantity": "20", "budget": "30000.00"})
    assert response.status_code == 201, response.text
    request_id = response.json()["id"]
    service, calls = client.app.state.service, []
    original = service.events
    def events(*args, **kwargs):
        calls.append(1)
        identities.revoke_user_sessions("synthetic-pilot", "buyer")
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "events", events)
    response = client.get(f"/api/v1/requests/{request_id}/events/stream", headers=bearer(result))
    assert response.status_code == 200
    assert "event: auth_invalid" in response.text and "event: audit" not in response.text
    assert response.text.count("auth_invalid") == 1 and calls == [1]


def test_pilot_cli_requires_explicit_apply_and_redacts_errors(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PF_", "ERP_", "LLM_"))}
    env.update(PYTHONPATH=str(ROOT / "services/api"), PF_MODE="pilot", PF_DATA_DIR=str(tmp_path),
               PF_DATABASE_URL=f"sqlite:///{tmp_path / 'cli.sqlite3'}")
    def run(*args, body="{}"):
        return subprocess.run([sys.executable, "-m", "procureflow.auth_cli", *args], cwd=ROOT, env=env,
                              input=body, text=True, capture_output=True, timeout=30)
    result = run("create-tenant", body='{"tenant_id":"test"}')
    assert result.returncode == 2 and not (tmp_path / "cli.sqlite3").exists()
    result = run("create-tenant", "--apply", body='{"password":"SENSITIVE"}')
    assert result.returncode == 1 and "SENSITIVE" not in result.stdout + result.stderr
    # Migration is an explicit separate operation, still within this temporary fixture.
    migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT / "services/api", env=env, text=True, capture_output=True, timeout=30)
    assert migrated.returncode == 0, migrated.stderr
    assert run("create-tenant", "--apply", body='{"tenant_id":"test"}').returncode == 0
    assert run("set-membership", "--apply", body='{"tenant_id":"test","user_id":"buyer","role":"buyer"}').returncode == 0
    output = tmp_path / "invite.txt"
    result = run("issue-invite", "--apply", "--credential-output", str(output),
        body='{"tenant_id":"test","user_id":"buyer"}')
    assert result.returncode == 0, result.stderr
    credential = output.read_text().strip()
    assert credential.startswith("pfi_") and credential not in result.stdout + result.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    assert "invite_id" in json.loads(result.stdout)
    again = run("issue-invite", "--apply", "--credential-output", str(output),
        body='{"tenant_id":"test","user_id":"buyer"}')
    assert again.returncode == 1 and output.read_text().strip() == credential
    with sqlite3.connect(tmp_path / "cli.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM pilot_invites").fetchone()[0] == 1
        assert credential not in str(connection.execute("SELECT * FROM pilot_invites").fetchall())


def test_foreign_origin_cannot_consume_invite_or_logout(identity_system):
    client, identities, _, _ = identity_system
    invite = identities.issue_invite("synthetic-pilot", "buyer")
    denied = client.post("/api/v1/auth/login", headers={"Origin": "https://attacker.invalid"},
                         json={"credential": invite["credential"]})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "ORIGIN_DENIED"
    accepted = client.post("/api/v1/auth/login", headers={"Origin": "http://localhost:3000"},
                           json={"credential": invite["credential"]})
    assert accepted.status_code == 200
    token = accepted.json()["token"]
    response = client.post("/api/v1/auth/logout", headers={
        "Origin": "https://attacker.invalid", "Authorization": "Bearer " + token})
    assert response.status_code == 403
    assert identities.authenticate(token).user_id == "buyer"
    # Same-origin browser use remains supported without an extra configured origin.
    response = client.post("/api/v1/auth/logout", headers={
        "Origin": "http://testserver", "Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert_denied(identities, token)


def test_membership_validation_and_noop_changes_preserve_authority(identity_system):
    client, identities, db, _ = identity_system
    result, _ = login(client, identities)
    original = identities.authenticate(result["token"])
    identities.set_membership("synthetic-pilot", "buyer", "buyer")
    identities.set_tenant_active("synthetic-pilot", True)
    assert identities.authenticate(result["token"]).auth_version == original.auth_version
    for expiry in ("2026-10-05", "2026-10-05T01:00:00", "bad", 42):
        with pytest.raises(ValueError, match="EXPIRY_INVALID"):
            identities.set_membership("synthetic-pilot", "buyer", "buyer", expires_at=expiry)
    with pytest.raises(ValueError, match="MEMBERSHIP_INVALID"):
        identities.set_membership("synthetic-pilot", "buyer", "administrator")
    with pytest.raises(ValueError, match="ID_INVALID"):
        identities.set_membership("synthetic-pilot", "bad\nuser", "buyer")
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    identities.set_membership("synthetic-pilot", "buyer", "buyer", expires_at=future)
    assert_denied(identities, result["token"])
    identities.set_membership("synthetic-pilot", "buyer", "buyer", active=False)
    with db.transaction() as session:
        assert session.get(PilotMembershipRow, ("synthetic-pilot", "buyer")).expires_at == future


def test_pilot_does_not_serve_legacy_static_demo(identity_system):
    client, _, _, _ = identity_system
    assert client.get("/").status_code == 404
    assert client.get("/assets/index.html").status_code == 404
    assert client.get("/health").status_code == 200


def test_legacy_static_demo_and_identity_shape_unchanged(system):
    client, _, _ = system
    assert client.get("/").status_code == 200
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer demo-buyer"}).json() == {
        "user_id": "buyer-01", "tenant_id": "demo", "role": "buyer"}
    assert client.post("/api/v1/auth/login", json={"credential": "pfi_" + "x" * 43}).status_code == 404
