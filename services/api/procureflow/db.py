from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from .database_config import normalize_database_url
from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid(prefix="") -> str:
    return prefix + uuid4().hex


class Base(DeclarativeBase):
    pass


class RequestRow(Base):
    __tablename__ = "procurement_requests"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    owner_id: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tenant_id", "request_id", "sha256", name="uq_document_request_hash"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("procurement_requests.id"))
    filename: Mapped[str] = mapped_column(String(160))
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(100))
    fragments: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class QuoteRow(Base):
    __tablename__ = "quotes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("procurement_requests.id"))
    current_version: Mapped[int] = mapped_column(Integer, default=1)


class QuoteVersionRow(Base):
    __tablename__ = "quote_versions"
    __table_args__ = (UniqueConstraint("quote_id", "version", name="uq_quote_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    version: Mapped[int] = mapped_column(Integer)
    values: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[dict] = mapped_column(JSON)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    confirmed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("procurement_requests.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    approver_id: Mapped[str] = mapped_column(String(80))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    note: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class OperationRow(Base):
    __tablename__ = "external_operations"
    __table_args__ = (UniqueConstraint("tenant_id", "request_id", name="uq_one_operation_per_request"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("procurement_requests.id"))
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"))
    payload: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    lease_until: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    remote_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class OutboxRow(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("external_operations.id"), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class EventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    actor_id: Mapped[str] = mapped_column(String(80))
    type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class AdviceRunRow(Base):
    """A durable advisory receipt, never an approval or an execution command."""
    __tablename__ = "advice_runs"
    __table_args__ = (UniqueConstraint("tenant_id", "request_id", "idempotency_key",
                                      name="uq_advice_request_key"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("procurement_requests.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(80))
    request_version: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    input_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class TenantPolicyRow(Base):
    """Tenant serialization anchor. Lock before any request aggregate lock."""
    __tablename__ = "tenant_policies"
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False)


class PolicyVersionRow(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "version", name="uq_tenant_policy_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant_policies.tenant_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    policy_hash: Mapped[str] = mapped_column(String(64))
    effective_at: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    created_by: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text)


class EvaluationRow(Base):
    """Append-only comparison receipt including inputs and the prepared proposal."""
    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("procurement_requests.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(80))
    request_version: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    input_snapshot: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


def _immutable_receipt(*_):
    raise ValueError("IMMUTABLE_HISTORY")


for _model in (PolicyVersionRow, EvaluationRow):
    event.listen(_model, "before_update", _immutable_receipt)
    event.listen(_model, "before_delete", _immutable_receipt)


class Database:
    def __init__(self, url: str, create_schema: bool = False):
        url = normalize_database_url(url)
        self.sqlite = make_url(url).get_backend_name() == "sqlite"
        options = {"check_same_thread": False, "timeout": 20} if self.sqlite else {"connect_timeout": 10}
        engine_options = {} if self.sqlite else {"isolation_level": "READ COMMITTED"}
        self.engine = create_engine(url, connect_args=options, pool_pre_ping=True,
                                    hide_parameters=True, **engine_options)
        if self.sqlite:
            @event.listens_for(self.engine, "connect")
            def sqlite_settings(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=20000")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        if create_schema:
            if not self.sqlite:
                raise ValueError("POSTGRES_REQUIRES_ALEMBIC_MIGRATIONS")
            Base.metadata.create_all(self.engine)

    def check_ready(self, require_migrations: bool = True) -> dict:
        """Read-only connection + table + migration check; never performs DDL."""
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            if not set(Base.metadata.tables) <= set(inspect(connection).get_table_names()):
                raise RuntimeError("DATABASE_SCHEMA_MISSING")
            if require_migrations:
                current = set(MigrationContext.configure(connection).get_current_heads())
                scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "alembic"))
                if current != set(scripts.get_heads()):
                    raise RuntimeError("DATABASE_MIGRATION_REQUIRED")
        return {"status": "ok", "database": "sqlite" if self.sqlite else "postgresql",
                "schema": "current" if require_migrations else "local-demo"}

    @contextmanager
    def transaction(self, write=False):
        with self.sessions() as session:
            try:
                if write and self.sqlite:
                    # Serializes local demo writes, including concurrent approvals/dispatch.
                    session.execute(text("BEGIN IMMEDIATE"))
                if not self.sqlite:
                    # Bound SQL waiting; do not retry business writes automatically.
                    session.execute(text("SELECT set_config('lock_timeout', '10s', true)"))
                    session.execute(text("SELECT set_config('statement_timeout', '30s', true)"))
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise


def audit(session: Session, principal, request_id, event_type, payload):
    session.add(EventRow(tenant_id=principal.tenant_id, actor_id=principal.user_id,
                         request_id=request_id, type=event_type, payload=payload))
