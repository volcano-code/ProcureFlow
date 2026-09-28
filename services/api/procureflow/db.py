from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
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


class Database:
    def __init__(self, url: str, create_schema: bool = False):
        self.sqlite = url.startswith("sqlite")
        self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 20} if self.sqlite else {}, pool_pre_ping=True)
        if self.sqlite:
            @event.listens_for(self.engine, "connect")
            def sqlite_settings(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=20000")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        if create_schema:
            Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self, write=False):
        with self.sessions() as session:
            try:
                if write and self.sqlite:
                    # Serializes local demo writes, including concurrent approvals/dispatch.
                    session.execute(text("BEGIN IMMEDIATE"))
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise


def audit(session: Session, principal, request_id, event_type, payload):
    session.add(EventRow(tenant_id=principal.tenant_id, actor_id=principal.user_id,
                         request_id=request_id, type=event_type, payload=payload))
