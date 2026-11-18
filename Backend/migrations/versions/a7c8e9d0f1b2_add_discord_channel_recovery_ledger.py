"""Create durable Discord recovery ledger without touching existing rows.

Revision ID: a7c8e9d0f1b2
Revises: f1a8c4d2e9b0
Do not execute on persistent databases without separately approved rollout.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7c8e9d0f1b2'
down_revision = 'f1a8c4d2e9b0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'discord_channel_recoveries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_key', sa.String(length=100), nullable=False),
        sa.Column('appointment_id', sa.Integer(), sa.ForeignKey('appointments.id'), nullable=False),
        sa.Column('channel_id', sa.String(length=32), nullable=True),
        sa.Column('recovery_reason', sa.String(length=32), nullable=False),
        sa.Column('recovery_status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('last_error_code', sa.String(length=48), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ux_discord_recoveries_event_key', 'discord_channel_recoveries', ['event_key'], unique=True)
    op.create_index('ix_discord_recoveries_appointment_id', 'discord_channel_recoveries', ['appointment_id'])
    op.create_index('ix_discord_recoveries_channel_id', 'discord_channel_recoveries', ['channel_id'])
    op.create_index('ix_discord_recoveries_pending', 'discord_channel_recoveries', ['recovery_status', 'id'])


def downgrade():
    op.drop_index('ix_discord_recoveries_pending', table_name='discord_channel_recoveries')
    op.drop_index('ix_discord_recoveries_channel_id', table_name='discord_channel_recoveries')
    op.drop_index('ix_discord_recoveries_appointment_id', table_name='discord_channel_recoveries')
    op.drop_index('ux_discord_recoveries_event_key', table_name='discord_channel_recoveries')
    op.drop_table('discord_channel_recoveries')
