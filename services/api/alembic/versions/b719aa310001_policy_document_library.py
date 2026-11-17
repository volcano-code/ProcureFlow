"""Versioned advisory reference library; no changes to executable rules."""
from alembic import op
import sqlalchemy as sa

revision = 'b719aa310001'
down_revision = 'a40c72de1902'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('policy_documents',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('tenant_id', sa.String(80), nullable=False),
        sa.Column('document_key', sa.String(64), nullable=False),
        sa.Column('latest_version', sa.Integer(), nullable=False),
        sa.UniqueConstraint('tenant_id', 'document_key', name='uq_policy_document_key'),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_policy_document_tenant'))
    op.create_index('ix_policy_documents_tenant_id', 'policy_documents', ['tenant_id'])
    columns = [sa.Column('id', sa.String(40), primary_key=True)]
    for name, size, nullable in [('tenant_id',80,False),('document_id',40,False),('title',160,False),
        ('source_label',200,False),('scope',40,False),('source_hash',64,False),('effective_at',40,False),
        ('expires_at',40,True),('created_at',40,False),('created_by',80,False),('reason',500,False),
        ('embedding_id',160,True),('revision_hash',64,False)]:
        columns.append(sa.Column(name, sa.String(size), nullable=nullable))
    columns += [sa.Column('version',sa.Integer(),nullable=False),sa.Column('content',sa.Text(),nullable=False),
        sa.Column('fragments',sa.JSON(),nullable=False),sa.Column('withdrawn',sa.Boolean(),nullable=False),
        sa.Column('embedding_dimension',sa.Integer(),nullable=False),sa.Column('embeddings',sa.JSON(),nullable=False)]
    op.create_table('policy_document_revisions', *columns,
        sa.UniqueConstraint('document_id','version',name='uq_policy_doc_revision'),
        sa.ForeignKeyConstraint(['document_id','tenant_id'],['policy_documents.id','policy_documents.tenant_id']))
    op.create_index('ix_policy_document_revisions_tenant_id','policy_document_revisions',['tenant_id'])
    op.create_index('ix_policy_document_revisions_document_id','policy_document_revisions',['document_id'])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM quote_candidate_runs WHERE status IN ('QUEUED','RUNNING')")).scalar_one():
        raise RuntimeError('CANDIDATE_DISPATCH_ACTIVE_DOWNGRADE_REFUSED')
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM policy_documents')).scalar_one():
        raise RuntimeError('POLICY_DOCUMENT_HISTORY_DOWNGRADE_REFUSED')
    op.drop_table('policy_document_revisions')
    op.drop_table('policy_documents')
