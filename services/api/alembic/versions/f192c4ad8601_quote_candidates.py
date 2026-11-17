"""Durable advisory quote candidates; never a procurement authorization.

Revision ID: f192c4ad8601
Revises: e731bb62c905
"""
from alembic import op
import sqlalchemy as sa
revision = 'f192c4ad8601'
down_revision = 'e731bb62c905'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('quote_candidate_runs',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('tenant_id', sa.String(80), nullable=False),
        sa.Column('request_id', sa.String(40), sa.ForeignKey('procurement_requests.id'), nullable=False),
        sa.Column('document_id', sa.String(40), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('creator_id', sa.String(80), nullable=False),
        sa.Column('idempotency_key', sa.String(80), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('input_snapshot', sa.JSON(), nullable=False),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('output', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('error_code', sa.String(80), nullable=True),
        sa.Column('quote_id', sa.String(40), sa.ForeignKey('quotes.id'), nullable=True),
        sa.Column('expires_at', sa.String(40), nullable=False),
        sa.Column('lease_until', sa.String(40), nullable=True),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.Column('completed_at', sa.String(40), nullable=True),
        sa.UniqueConstraint('tenant_id', 'request_id', 'idempotency_key', name='uq_candidate_run_key'))
    op.create_index('ix_quote_candidate_runs_tenant_id', 'quote_candidate_runs', ['tenant_id'])
    op.create_index('ix_quote_candidate_runs_request_id', 'quote_candidate_runs', ['request_id'])


def downgrade():
    # Destructive to candidate history; never run on unbacked production data.
    op.drop_table('quote_candidate_runs')
