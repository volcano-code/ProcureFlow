"""Durable, tenant-scoped table import previews and idempotent confirmation receipts.

Revision ID: b9134d27c80f
Revises: a82d71f09c36
"""
from alembic import op
import sqlalchemy as sa

revision = "b9134d27c80f"
down_revision = "a82d71f09c36"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("table_imports",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("request_id", sa.String(40), sa.ForeignKey("procurement_requests.id"), nullable=False),
        sa.Column("filename", sa.String(160), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(100), nullable=False),
        sa.Column("table", sa.JSON(), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=True),
        sa.Column("parsed", sa.JSON(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("request_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("quote_id", sa.String(40), sa.ForeignKey("quotes.id"), nullable=True),
        sa.Column("expires_at", sa.String(40), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("tenant_id", "request_id", "sha256", name="uq_table_import_request_hash"))
    op.create_index("ix_table_imports_tenant_id", "table_imports", ["tenant_id"])
    op.create_index("ix_table_imports_request_id", "table_imports", ["request_id"])


def downgrade():
    op.drop_index("ix_table_imports_request_id", table_name="table_imports")
    op.drop_index("ix_table_imports_tenant_id", table_name="table_imports")
    op.drop_table("table_imports")
