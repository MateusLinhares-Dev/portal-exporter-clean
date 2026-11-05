"""Per-job navigation/document audit and generated PDF report reference.

Revision ID: 20261029_0002
Revises: 20261022_0001
"""
from alembic import op
import sqlalchemy as sa

revision = '20261029_0002'
down_revision = '20261022_0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('sync_jobs', sa.Column('report_path', sa.Text(), nullable=True))
    op.add_column('sync_jobs', sa.Column('report_generated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('sync_jobs', sa.Column('report_error', sa.Text(), nullable=True))
    op.create_table('job_audit_events',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('job_id', sa.String(36), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('tab', sa.Text(), nullable=False),
        sa.Column('portal_path', sa.Text(), nullable=False),
        sa.Column('document_id', sa.String(255), nullable=False),
        sa.Column('document_code', sa.Text(), nullable=False),
        sa.Column('document_date', sa.Text(), nullable=False),
        sa.Column('unit', sa.Text(), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('file_name', sa.Text(), nullable=False),
        sa.Column('destination_path', sa.Text(), nullable=False),
        sa.Column('size_bytes', sa.BigInteger()),
        sa.Column('checksum_sha256', sa.String(64), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_job_audit_events_job_id', 'job_audit_events', ['job_id'])


def downgrade() -> None:
    op.drop_index('ix_job_audit_events_job_id', table_name='job_audit_events')
    op.drop_table('job_audit_events')
    op.drop_column('sync_jobs', 'report_error')
    op.drop_column('sync_jobs', 'report_generated_at')
    op.drop_column('sync_jobs', 'report_path')
