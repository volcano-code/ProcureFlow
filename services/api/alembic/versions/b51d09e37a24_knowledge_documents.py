"""Immutable tenant document revisions; evidence only, no enforcement change.

Revision ID: b51d09e37a24
Revises: a40c72de1902
"""
from alembic import op
import sqlalchemy as sa

revision = "b51d09e37a24"
down_revision = "a40c72de1902"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_documents",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("document_key", sa.String(80), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "document_key", name="uq_knowledge_document_key"))
    op.create_index("ix_knowledge_documents_tenant_id", "knowledge_documents", ["tenant_id"])
    op.create_table("knowledge_versions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("document_id", sa.String(40), sa.ForeignKey("knowledge_documents.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("scope", sa.String(80), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("chunks", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("effective_at", sa.String(40)),
        sa.Column("expires_at", sa.String(40)),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("published_by", sa.String(80)),
        sa.Column("retired_at", sa.String(40)),
        sa.UniqueConstraint("document_id", "version", name="uq_knowledge_version"))
    op.create_index("ix_knowledge_versions_tenant_id", "knowledge_versions", ["tenant_id"])
    op.create_index("ix_knowledge_versions_document_id", "knowledge_versions", ["document_id"])


def downgrade():
    # Evidence is not disposable merely because it is not a purchase transaction.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM knowledge_versions")).scalar_one():
        raise RuntimeError("KNOWLEDGE_DATA_DOWNGRADE_REFUSED")
    op.drop_index("ix_knowledge_versions_document_id", table_name="knowledge_versions")
    op.drop_index("ix_knowledge_versions_tenant_id", table_name="knowledge_versions")
    op.drop_table("knowledge_versions")
    op.drop_index("ix_knowledge_documents_tenant_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
