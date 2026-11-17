"""Controlled pilot identities and short-lived, hash-only bearer sessions.

No account is provisioned by importing this module or starting the API. Operators
use the offline CLI after a separate authorization step. All pilot authority
changes serialize on the existing policy tenant anchor, before identity locks;
this is also the lock the business write gate holds across the first ERP POST.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets

from sqlalchemy import select

from .contracts import Principal
from .db import (PilotInviteRow, PilotMembershipRow, PilotSessionRow, PilotTenantRow,
                 audit, now, uid)
from .errors import DomainError
from .policies import bootstrap, lock_tenant

ROLES = {"buyer", "approver", "auditor"}
_UNSET = object()


def secret_hash(value: str) -> str:
    # Random 256-bit credentials need a one-way digest, not password stretching.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def unauthenticated():
    return DomainError("UNAUTHENTICATED", "Credential or session is no longer valid; sign in again", 401)


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _unexpired(value: str) -> bool:
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.tzinfo is not None and stamp > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _version(tenant, member) -> str:
    return f"{tenant.generation}:{member.generation}"


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}", value):
        raise ValueError("IDENTITY_ID_INVALID")
    return value


def _read(session, query, lock=False):
    # Authority must not come from a stale SQLAlchemy identity map.
    query = query.execution_options(populate_existing=True)
    return session.scalar(query.with_for_update() if lock else query)


class IdentityService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def _pilot(self):
        if self.settings.mode != "pilot":
            raise DomainError("PILOT_AUTH_DISABLED", "Controlled pilot login is not enabled", 404)

    def _authority(self, session, tenant_id, user_id, lock=False):
        if lock:
            # Always before identity/request/operation locks, including revocation.
            lock_tenant(session, tenant_id)
        tenant = _read(session, select(PilotTenantRow).where(PilotTenantRow.tenant_id == tenant_id), lock)
        member = _read(session, select(PilotMembershipRow).where(
            PilotMembershipRow.tenant_id == tenant_id, PilotMembershipRow.user_id == user_id), lock)
        if (tenant is None or not tenant.active or member is None or not member.active or member.role not in ROLES
                or (member.expires_at is not None and not _unexpired(member.expires_at))):
            raise unauthenticated()
        return tenant, member

    def _principal(self, member, row):
        principal = Principal(user_id=member.user_id, tenant_id=member.tenant_id, role=member.role)
        principal._session_id = row.id
        principal._auth_version = row.auth_version
        principal._session_expires_at = row.expires_at
        return principal

    def authenticate(self, token: str) -> Principal:
        if self.settings.mode != "pilot":
            for configured, principal in self.settings.auth_tokens.items():
                if secrets.compare_digest(configured.encode(), token.encode()):
                    return Principal.model_validate(principal)
            raise unauthenticated()
        if not isinstance(token, str) or len(token) > 256 or not token.startswith("pfs_"):
            raise unauthenticated()
        with self.db.transaction() as session:
            row = _read(session, select(PilotSessionRow).where(PilotSessionRow.token_hash == secret_hash(token)))
            if row is None:
                raise unauthenticated()
            tenant, member = self._authority(session, row.tenant_id, row.user_id)
            if row.revoked_at or not _unexpired(row.expires_at) or row.auth_version != _version(tenant, member):
                raise unauthenticated()
            return self._principal(member, row)

    def validate_principal(self, session, principal, lock=False) -> Principal:
        """Revalidate in the exact business transaction, retaining locks on writes."""
        if self.settings.mode != "pilot":
            return principal  # Explicitly bounded legacy static-token/demo mode.
        if not principal.session_id or not principal.auth_version:
            raise unauthenticated()
        tenant, member = self._authority(session, principal.tenant_id, principal.user_id, lock)
        row = _read(session, select(PilotSessionRow).where(PilotSessionRow.id == principal.session_id), lock)
        if (row is None or row.tenant_id != principal.tenant_id or row.user_id != principal.user_id
                or member.role != principal.role or row.revoked_at or not _unexpired(row.expires_at)
                or row.auth_version != _version(tenant, member) or row.auth_version != principal.auth_version):
            raise unauthenticated()
        return self._principal(member, row)

    def approver_valid(self, session, tenant_id, user_id, auth_version=None, lock=False) -> bool:
        return self.actor_valid(session, tenant_id, user_id, "approver", auth_version, lock)

    def actor_valid(self, session, tenant_id, user_id, role, auth_version=None, lock=False) -> bool:
        if self.settings.mode != "pilot":
            return any(item["tenant_id"] == tenant_id and item["user_id"] == user_id and item["role"] == role
                       for item in self.settings.auth_tokens.values())
        if not auth_version:
            return False  # Legacy approvals are never silently upgraded into pilot authority.
        try:
            tenant, member = self._authority(session, tenant_id, user_id, lock)
        except DomainError:
            return False
        return member.role == role and auth_version == _version(tenant, member)

    def login(self, credential: str) -> dict:
        self._pilot()
        if not isinstance(credential, str) or len(credential) > 256 or not credential.startswith("pfi_"):
            raise unauthenticated()
        with self.db.transaction(write=True) as session:
            # A read finds the tenant only. Re-read after acquiring its lock.
            candidate = _read(session, select(PilotInviteRow).where(
                PilotInviteRow.credential_hash == secret_hash(credential)))
            if candidate is None:
                raise unauthenticated()
            tenant, member = self._authority(session, candidate.tenant_id, candidate.user_id, lock=True)
            invite = _read(session, select(PilotInviteRow).where(PilotInviteRow.id == candidate.id), lock=True)
            if (invite.consumed_at or invite.revoked_at or not _unexpired(invite.expires_at)
                    or invite.auth_version != _version(tenant, member)):
                raise unauthenticated()
            token = "pfs_" + secrets.token_urlsafe(32)
            row = PilotSessionRow(id=uid("sess_"), tenant_id=member.tenant_id, user_id=member.user_id,
                token_hash=secret_hash(token), auth_version=_version(tenant, member),
                expires_at=_future(self.settings.session_ttl_seconds))
            invite.consumed_at = now()
            session.add(row)
            session.flush()
            principal = self._principal(member, row)
            audit(session, principal, None, "PILOT_SESSION_STARTED", {"session_id": row.id})
            return {"token": token, "token_type": "bearer", "expires_at": row.expires_at,
                    "identity": principal.model_dump()}

    def logout(self, principal):
        if self.settings.mode != "pilot":
            return
        with self.db.transaction(write=True) as session:
            self.validate_principal(session, principal, lock=True)
            row = session.get(PilotSessionRow, principal.session_id)
            row.revoked_at = now()
            audit(session, principal, None, "PILOT_SESSION_ENDED", {"session_id": row.id})

    # Offline management methods. None are routed through the public API.
    def _operator_audit(self, session, tenant_id, kind, payload):
        actor = Principal(tenant_id=tenant_id, user_id="offline-operator", role="auditor")
        audit(session, actor, None, kind, payload)

    def create_tenant(self, tenant_id):
        self._pilot()
        _identifier(tenant_id)
        with self.db.transaction(write=True) as session:
            bootstrap(session, tenant_id)
            lock_tenant(session, tenant_id)
            if session.get(PilotTenantRow, tenant_id):
                raise ValueError("TENANT_ALREADY_EXISTS")
            session.add(PilotTenantRow(tenant_id=tenant_id, active=True, generation=1))
            self._operator_audit(session, tenant_id, "PILOT_TENANT_CREATED", {})
        return {"tenant_id": tenant_id, "active": True}

    def set_tenant_active(self, tenant_id, active):
        self._pilot()
        if not isinstance(active, bool):
            raise ValueError("ACTIVE_MUST_BE_BOOLEAN")
        with self.db.transaction(write=True) as session:
            lock_tenant(session, tenant_id)
            tenant = _read(session, select(PilotTenantRow).where(PilotTenantRow.tenant_id == tenant_id), True)
            if tenant is None:
                raise ValueError("TENANT_NOT_FOUND")
            if tenant.active != active:
                tenant.active, tenant.generation = active, tenant.generation + 1
                self._operator_audit(session, tenant_id, "PILOT_TENANT_CHANGED", {"active": active})
        return {"tenant_id": tenant_id, "active": active}

    def set_membership(self, tenant_id, user_id, role, active=True, expires_at=_UNSET):
        self._pilot()
        _identifier(tenant_id)
        _identifier(user_id)
        if role not in ROLES or not isinstance(active, bool):
            raise ValueError("MEMBERSHIP_INVALID")
        if expires_at is not _UNSET and expires_at is not None:
            try:
                expiry = datetime.fromisoformat(expires_at)
                if expiry.tzinfo is None:
                    raise ValueError()
                expires_at = expiry.astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                raise ValueError("MEMBERSHIP_EXPIRY_INVALID") from None
        with self.db.transaction(write=True) as session:
            lock_tenant(session, tenant_id)
            tenant = _read(session, select(PilotTenantRow).where(PilotTenantRow.tenant_id == tenant_id), True)
            if tenant is None:
                raise ValueError("TENANT_NOT_FOUND")
            member = _read(session, select(PilotMembershipRow).where(
                PilotMembershipRow.tenant_id == tenant_id, PilotMembershipRow.user_id == user_id), True)
            if member is None:
                member = PilotMembershipRow(tenant_id=tenant_id, user_id=user_id, role=role, active=active, generation=1,
                    expires_at=None if expires_at is _UNSET else expires_at)
                session.add(member)
            elif (member.role != role or member.active != active
                    or (expires_at is not _UNSET and member.expires_at != expires_at)):
                member.role, member.active, member.generation = role, active, member.generation + 1
                if expires_at is not _UNSET:
                    member.expires_at = expires_at
            else:
                return {"tenant_id": tenant_id, "user_id": user_id, "role": role, "active": active}
            self._operator_audit(session, tenant_id, "PILOT_MEMBERSHIP_CHANGED", {
                "user_id": user_id, "role": role, "active": active, "generation": member.generation, "expires_at": member.expires_at})
        return {"tenant_id": tenant_id, "user_id": user_id, "role": role, "active": active}

    def issue_invite(self, tenant_id, user_id, ttl_seconds=None):
        self._pilot()
        ttl = self.settings.invite_ttl_seconds if ttl_seconds is None else ttl_seconds
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 60 <= ttl <= 604800:
            raise ValueError("INVITE_TTL_INVALID")
        with self.db.transaction(write=True) as session:
            tenant, member = self._authority(session, tenant_id, user_id, lock=True)
            credential = "pfi_" + secrets.token_urlsafe(32)
            row = PilotInviteRow(id=uid("inv_"), tenant_id=tenant_id, user_id=user_id,
                credential_hash=secret_hash(credential), auth_version=_version(tenant, member), expires_at=_future(ttl))
            session.add(row)
            self._operator_audit(session, tenant_id, "PILOT_INVITE_ISSUED", {
                "invite_id": row.id, "user_id": user_id, "expires_at": row.expires_at})
            return {"credential": credential, "invite_id": row.id, "expires_at": row.expires_at}

    def _revoke(self, model, record_id):
        self._pilot()
        with self.db.transaction(write=True) as session:
            candidate = session.get(model, record_id)
            if candidate is None:
                raise ValueError("AUTH_RECORD_NOT_FOUND")
            lock_tenant(session, candidate.tenant_id)
            row = _read(session, select(model).where(model.id == record_id), True)
            row.revoked_at = row.revoked_at or now()
            self._operator_audit(session, row.tenant_id, "PILOT_AUTH_REVOKED", {"record_id": row.id})
        return {"status": "revoked"}

    def revoke_invite(self, invite_id):
        return self._revoke(PilotInviteRow, invite_id)

    def revoke_session(self, session_id):
        return self._revoke(PilotSessionRow, session_id)

    def revoke_user_sessions(self, tenant_id, user_id):
        self._pilot()
        with self.db.transaction(write=True) as session:
            lock_tenant(session, tenant_id)
            rows = session.scalars(select(PilotSessionRow).where(PilotSessionRow.tenant_id == tenant_id,
                PilotSessionRow.user_id == user_id, PilotSessionRow.revoked_at.is_(None)).with_for_update())
            count, timestamp = 0, now()
            for row in rows:
                row.revoked_at = timestamp
                count += 1
            self._operator_audit(session, tenant_id, "PILOT_USER_SESSIONS_REVOKED", {"user_id": user_id, "count": count})
        return {"status": "revoked", "count": count}
