"""Add durable manual operator review audit; no automatic delete permissions.

Revision ID: b8d9e0f1a2c3
Revises: a7c8e9d0f1b2
"""
from alembic import op
import sqlalchemy as sa
revision = "b8d9e0f1a2c3"
down_revision = "a7c8e9d0f1b2"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("discord_channel_recoveries") as batch:
        batch.add_column(sa.Column("operator_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("operator_notes", sa.Text(), nullable=True))
        batch.add_column(sa.Column("resolved_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("review_version", sa.Integer(), nullable=False, server_default="0"))
        batch.create_foreign_key("fk_recovery_operator", "users", ["operator_id"], ["id"])

def downgrade():
    with op.batch_alter_table("discord_channel_recoveries") as batch:
        batch.drop_constraint("fk_recovery_operator", type_="foreignkey")
        batch.drop_column("review_version")
        batch.drop_column("resolved_at")
        batch.drop_column("operator_notes")
        batch.drop_column("operator_id")
