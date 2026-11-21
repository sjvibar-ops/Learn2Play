"""Preserve attendance actions in an append-only application audit.

Revision ID: a8f2b6c01d73
Revises: e6a1c9d84b20
"""
from alembic import op
import sqlalchemy as sa

revision = 'a8f2b6c01d73'
down_revision = 'e6a1c9d84b20'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('appointments') as batch:
        batch.add_column(sa.Column('attendance_resolved_at', sa.DateTime()))
        batch.add_column(sa.Column('attendance_resolved_by_id', sa.Integer()))
        batch.create_foreign_key('fk_appointments_attendance_resolved_by_id_users', 'users', ['attendance_resolved_by_id'], ['id'])
        batch.add_column(sa.Column('attendance_resolution', sa.String(30)))
        batch.add_column(sa.Column('attendance_resolution_reason', sa.String(1000)))
        batch.add_column(sa.Column('attendance_closed_at', sa.DateTime()))
    op.create_table('attendance_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('appointment_id', sa.Integer(), sa.ForeignKey('appointments.id'), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('action', sa.String(40), nullable=False),
        sa.Column('note', sa.String(1000), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_attendance_events_appointment_id', 'attendance_events', ['appointment_id'])


def downgrade():
    raise RuntimeError('Attendance audit history must be retained. Use a reviewed forward migration.')
