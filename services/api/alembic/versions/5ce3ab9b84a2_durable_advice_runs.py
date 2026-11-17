"""durable version-bound read-only advice receipts
Revision ID: 5ce3ab9b84a2
Revises: 426d852ce82c
"""
from alembic import op
import sqlalchemy as sa

revision = "5ce3ab9b84a2"
down_revision = "426d852ce82c"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("advice_runs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("request_id", sa.String(40), sa.ForeignKey("procurement_requests.id"), nullable=False),
        sa.Column("actor_id", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("request_version", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("lease_until", sa.String(40), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("started_at", sa.String(40), nullable=True),
        sa.Column("completed_at", sa.String(40), nullable=True),
        sa.UniqueConstraint("tenant_id", "request_id", "idempotency_key", name="uq_advice_request_key"))
    op.create_index("ix_advice_runs_tenant_id", "advice_runs", ["tenant_id"])
    op.create_index("ix_advice_runs_request_id", "advice_runs", ["request_id"])


def downgrade():
    op.drop_index("ix_advice_runs_request_id", table_name="advice_runs")
    op.drop_index("ix_advice_runs_tenant_id", table_name="advice_runs")
    op.drop_table("advice_runs")
