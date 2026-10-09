"""Durable maintenance fence and permanent no-replay holds for restored work.

Revision ID: e731bb62c905
Revises: d261a40ce712
"""
from datetime import datetime, timezone
from alembic import op
import sqlalchemy as sa

revision = "e731bb62c905"
down_revision = "d261a40ce712"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table("system_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(120), nullable=False),
        sa.Column("restore_id", sa.String(64), nullable=True),
        sa.Column("required_auth_mode", sa.String(24), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False))
    op.bulk_insert(table, [{"id": 1, "state": "ACTIVE", "generation": 1,
        "reason": "initial", "restore_id": None, "required_auth_mode": None,
        "updated_at": datetime.now(timezone.utc).isoformat()}])
    op.create_table("recovery_holds",
        sa.Column("operation_id", sa.String(64), sa.ForeignKey("external_operations.id"), primary_key=True),
        sa.Column("restore_id", sa.String(64), nullable=False),
        sa.Column("original_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False))


def downgrade():
    # Never erase a restore fence to regain old dispatch behavior.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT count(*) FROM recovery_holds")).scalar() or connection.execute(
            sa.text("SELECT count(*) FROM system_state WHERE state != 'ACTIVE' OR restore_id IS NOT NULL")).scalar():
        raise RuntimeError("RECOVERY_FENCE_DOWNGRADE_FORBIDDEN")
    op.drop_table("recovery_holds")
    op.drop_table("system_state")
