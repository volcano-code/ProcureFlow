"""Immutable tenant policy revisions and complete historical evaluation inputs.

Revision ID: a82d71f09c36
Revises: 5ce3ab9b84a2
"""
from alembic import op
import sqlalchemy as sa

revision = "a82d71f09c36"
down_revision = "5ce3ab9b84a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("tenant_policies",
        sa.Column("tenant_id", sa.String(80), primary_key=True),
        sa.Column("latest_version", sa.Integer(), nullable=False))
    op.create_table("policy_versions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(80), sa.ForeignKey("tenant_policies.tenant_id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.String(40), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.UniqueConstraint("tenant_id", "version", name="uq_tenant_policy_version"))
    op.create_index("ix_policy_versions_tenant_id", "policy_versions", ["tenant_id"])
    op.create_table("evaluations",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("request_id", sa.String(40), sa.ForeignKey("procurement_requests.id"), nullable=False),
        sa.Column("actor_id", sa.String(80), nullable=False),
        sa.Column("request_version", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False))
    op.create_index("ix_evaluations_tenant_id", "evaluations", ["tenant_id"])
    op.create_index("ix_evaluations_request_id", "evaluations", ["request_id"])
    # Legacy advice/approvals cannot be truthfully reconstructed. Preserve their
    # original receipts with NULL inputs; runtime marks them unbound/stale.
    op.add_column("advice_runs", sa.Column("input_snapshot", sa.JSON(), nullable=True))
    op.add_column("approvals", sa.Column("snapshot", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("approvals", "snapshot")
    op.drop_column("advice_runs", "input_snapshot")
    op.drop_index("ix_evaluations_request_id", table_name="evaluations")
    op.drop_index("ix_evaluations_tenant_id", table_name="evaluations")
    op.drop_table("evaluations")
    op.drop_index("ix_policy_versions_tenant_id", table_name="policy_versions")
    op.drop_table("policy_versions")
    op.drop_table("tenant_policies")
