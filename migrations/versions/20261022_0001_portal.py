"""Initial schema for standalone portal-only exporter; no Dataset or callback tables.

Revision ID: 20261022_0001
Revises:
"""
from alembic import op
import sqlalchemy as sa
revision = '20261022_0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table('sync_jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('requested_by', sa.String(120), nullable=False),
        sa.Column('mode', sa.String(20), nullable=False),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('schedule_key', sa.String(100), nullable=True, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True)),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('total_records', sa.Integer(), nullable=False),
        sa.Column('downloaded', sa.Integer(), nullable=False),
        sa.Column('skipped', sa.Integer(), nullable=False),
        sa.Column('failed', sa.Integer(), nullable=False),
        sa.Column('error_summary', sa.Text()),
    )
    op.create_index('ix_sync_jobs_status', 'sync_jobs', ['status'])
    op.create_table('extractions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.String(36), nullable=False),
        sa.Column('source_key', sa.String(64), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('document_id', sa.String(255), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('portal_path', sa.Text(), nullable=False),
        sa.Column('file_name', sa.Text(), nullable=False),
        sa.Column('tab', sa.Text(), nullable=False),
        sa.Column('unit', sa.Text(), nullable=False),
        sa.Column('destination_path', sa.Text()),
        sa.Column('checksum_sha256', sa.String(64)),
        sa.Column('size_bytes', sa.BigInteger()),
        sa.Column('error_message', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('source_key', name='uq_extractions_source_key'),
    )
    op.create_index('ix_extractions_status', 'extractions', ['status'])
    op.create_index('ix_extractions_job_id', 'extractions', ['job_id'])

def downgrade() -> None:
    op.drop_index('ix_extractions_job_id', table_name='extractions')
    op.drop_index('ix_extractions_status', table_name='extractions')
    op.drop_table('extractions')
    op.drop_index('ix_sync_jobs_status', table_name='sync_jobs')
    op.drop_table('sync_jobs')
