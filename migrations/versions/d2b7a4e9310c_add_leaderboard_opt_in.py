"""add leaderboard opt-in preference

Revision ID: d2b7a4e9310c
Revises: f31a6e8c9b24
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa


revision = "d2b7a4e9310c"
down_revision = "f31a6e8c9b24"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column(
            "leaderboard_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()
        ))


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("leaderboard_opt_in")
