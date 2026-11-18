"""Durable audit tracking for Discord deletion dispatch.

Revision ID: d0a1b2c3d4e5
Revises: c9e0f1a2b3d4
"""
from alembic import op
import sqlalchemy as sa
revision = 'd0a1b2c3d4e5'
down_revision = 'c9e0f1a2b3d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('discord_channel_recoveries') as batch:
        batch.add_column(sa.Column('deletion_dispatched_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('deletion_http_status', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('discord_channel_recoveries') as batch:
        batch.drop_column('deletion_http_status')
        batch.drop_column('deletion_dispatched_at')
