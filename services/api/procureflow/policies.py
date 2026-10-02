"""Immutable tenant policy revisions, selected by monotonic effective version.

Every business writer locks the tenant anchor before a request. Publication uses
that same anchor. A future version is effective at a gate's wall-clock time; an
older scheduled revision can never replace a higher already-effective revision.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from sqlalchemy import select
from .db import PolicyVersionRow, TenantPolicyRow, audit, now, uid
from .domain import digest
from .errors import DomainError

# These deterministic invariants are versioned into each immutable policy body.
CLAUSES = [
    {"id": "P-01", "text": "Only CNY, identical SKU, EA unit and identical requested quantity can be compared."},
    {"id": "P-02", "text": "Shipping and discounts must be explicit; unknown is not zero. Tax status must be known."},
    {"id": "P-03", "text": "The total and delivery must satisfy both request limits and any tenant caps."},
    {"id": "P-04", "text": "A different authorized approver must approve an unchanged complete quote collection and effective policy before a draft write."},
    {"id": "P-05", "text": "The minimum valid quote count is the number of distinct supplier IDs with confirmed offers satisfying every deterministic rule."},
]


def immutable_policy(row):
    return {"id": row.id, "tenant_id": row.tenant_id, "version": row.version,
            **deepcopy(row.data), "effective_at": row.effective_at, "created_at": row.created_at,
            "created_by": row.created_by, "reason": row.reason, "policy_hash": row.policy_hash}


def make_version(tenant_id, version, data, effective_at, actor, reason):
    row = PolicyVersionRow(id=uid("pol_"), tenant_id=tenant_id, version=version,
        data={**data, "clauses": deepcopy(CLAUSES)}, effective_at=effective_at,
        created_at=now(), created_by=actor, reason=reason, policy_hash="")
    body = immutable_policy(row)
    row.policy_hash = digest({k: v for k, v in body.items() if k != "policy_hash"})
    return row


def bootstrap(session, tenant_id):
    # Atomic insert handles two service processes bootstrapping the same tenant.
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    created = session.execute(insert(TenantPolicyRow).values(tenant_id=tenant_id, latest_version=1)
        .on_conflict_do_nothing(index_elements=["tenant_id"]).returning(TenantPolicyRow.tenant_id)).scalar()
    if created:
        session.add(make_version(tenant_id, 1, {"budget_cap": None, "max_delivery_days": None,
            "minimum_valid_quotes": 1}, "1970-01-01T00:00:00+00:00", "system", "Initial tenant policy; request limits remain in force"))
        session.flush()


def lock_tenant(session, tenant_id):
    row = session.scalar(select(TenantPolicyRow).where(TenantPolicyRow.tenant_id == tenant_id)
                         .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise DomainError("POLICY_NOT_INITIALIZED", "Tenant policy has not been initialized", 503)
    return row


def effective_policy(session, tenant_id, at=None):
    row = session.scalar(select(PolicyVersionRow).where(PolicyVersionRow.tenant_id == tenant_id,
        PolicyVersionRow.effective_at <= (at or now())).order_by(PolicyVersionRow.version.desc()).limit(1))
    if row is None:
        raise DomainError("POLICY_NOT_INITIALIZED", "No effective tenant policy exists", 503)
    return immutable_policy(row)


def policy_versions(session, tenant_id):
    at = now()
    active = effective_policy(session, tenant_id, at)
    latest = session.get(TenantPolicyRow, tenant_id).latest_version
    return [{**immutable_policy(row), "latest_version": latest,
             "status": "effective" if row.version == active["version"] else
                       "scheduled" if row.version > active["version"] and row.effective_at > at else "superseded"}
            for row in session.scalars(select(PolicyVersionRow).where(PolicyVersionRow.tenant_id == tenant_id)
                                       .order_by(PolicyVersionRow.version.desc()))]


def publish(session, principal, command):
    tenant = lock_tenant(session, principal.tenant_id)
    if tenant.latest_version != command.expected_version:
        raise DomainError("VERSION_CONFLICT", "A newer policy exists. Refresh policy history before publishing")
    timestamp = datetime.now(timezone.utc)
    effective_at = command.effective_at.astimezone(timezone.utc) if command.effective_at else timestamp
    if effective_at < timestamp:
        raise DomainError("POLICY_BACKDATE_DENIED", "Policy revisions cannot be backdated", 422)
    row = make_version(principal.tenant_id, tenant.latest_version + 1,
        command.model_dump(mode="json", include={"budget_cap", "max_delivery_days", "minimum_valid_quotes"}),
        effective_at.isoformat(), principal.user_id, command.reason)
    session.add(row)
    tenant.latest_version = row.version
    session.flush()
    audit(session, principal, None, "POLICY_VERSION_PUBLISHED", {"policy_id": row.id,
        "policy_version": row.version, "policy_hash": row.policy_hash, "effective_at": row.effective_at,
        "reason": row.reason})
    return row
