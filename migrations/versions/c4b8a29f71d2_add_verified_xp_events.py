"""add verified XP events

Revision ID: c4b8a29f71d2
Revises: 9e45f7e7aa32
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa


revision = "c4b8a29f71d2"
down_revision = "9e45f7e7aa32"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "xp_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("appointment_id", sa.Integer(), sa.ForeignKey("appointments.id"), nullable=False),
        sa.Column("path", sa.String(length=20), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("points > 0", name="ck_xp_events_positive_points"),
        sa.CheckConstraint("path IN ('academic', 'gaming')", name="ck_xp_events_path"),
        sa.UniqueConstraint("appointment_id", name="uq_xp_events_appointment"),
    )
    op.create_index("ix_xp_events_user_created", "xp_events", ["user_id", "created_at"])


def downgrade():
    op.drop_index("ix_xp_events_user_created", table_name="xp_events")
    op.drop_table("xp_events")
