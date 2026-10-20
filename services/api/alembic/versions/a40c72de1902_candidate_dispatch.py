"""Persist explicit candidate dispatch authority without bearer secrets.

Revision ID: a40c72de1902
Revises: f192c4ad8601
"""
from alembic import op
import sqlalchemy as sa

revision = 'a40c72de1902'
down_revision = 'f192c4ad8601'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('quote_candidate_runs', sa.Column('queued_at', sa.String(40), nullable=True))
    op.add_column('quote_candidate_runs', sa.Column('dispatch_authority', sa.JSON(), nullable=True))
    op.create_index('ix_candidate_dispatch', 'quote_candidate_runs', ['status', 'created_at', 'id'])


def downgrade():
    # Stop all API/worker processes first. Never silently strand accepted work.
    if op.get_bind().execute(sa.text(
            "SELECT COUNT(*) FROM quote_candidate_runs WHERE status IN ('QUEUED', 'RUNNING')"
            )).scalar_one():
        raise RuntimeError('CANDIDATE_DISPATCH_ACTIVE_DOWNGRADE_REFUSED')
    op.drop_index('ix_candidate_dispatch', table_name='quote_candidate_runs')
    op.drop_column('quote_candidate_runs', 'dispatch_authority')
    op.drop_column('quote_candidate_runs', 'queued_at')
