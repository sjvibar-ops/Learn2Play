"""add two-sided session attendance confirmations

Revision ID: f31a6e8c9b24
Revises: c4b8a29f71d2
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa


revision = "f31a6e8c9b24"
down_revision = "c4b8a29f71d2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("appointments") as batch_op:
        batch_op.add_column(sa.Column("provider_confirmed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("student_confirmed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("attendance_disputed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("attendance_disputed_by_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_appointments_attendance_disputed_by_id_users",
            "users",
            ["attendance_disputed_by_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("appointments") as batch_op:
        batch_op.drop_constraint("fk_appointments_attendance_disputed_by_id_users", type_="foreignkey")
        batch_op.drop_column("attendance_disputed_by_id")
        batch_op.drop_column("attendance_disputed_at")
        batch_op.drop_column("student_confirmed_at")
        batch_op.drop_column("provider_confirmed_at")
