"""Durable audit-only cleanup review claim; no delete permission.

Revision ID: c9e0f1a2b3d4
Revises: b8d9e0f1a2c3
"""
from alembic import op
import sqlalchemy as sa
revision = 'c9e0f1a2b3d4'
down_revision = 'b8d9e0f1a2c3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('discord_channel_recoveries') as batch:
        batch.add_column(sa.Column('cleanup_fencing_token', sa.String(64), nullable=True))
        batch.add_column(sa.Column('cleanup_state', sa.String(32), nullable=False, server_default='UNCLAIMED'))
        batch.add_column(sa.Column('cleanup_claimed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('cleanup_worker_id', sa.String(64), nullable=True))
        batch.create_index('ix_recoveries_cleanup_state', ['cleanup_state'])


def downgrade():
    with op.batch_alter_table('discord_channel_recoveries') as batch:
        batch.drop_index('ix_recoveries_cleanup_state')
        batch.drop_column('cleanup_worker_id')
        batch.drop_column('cleanup_claimed_at')
        batch.drop_column('cleanup_state')
        batch.drop_column('cleanup_fencing_token')
