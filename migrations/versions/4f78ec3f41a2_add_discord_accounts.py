"""Add optional Discord account links."""

from alembic import op
import sqlalchemy as sa

revision = "4f78ec3f41a2"
down_revision = "db71a6e5c902"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discord_accounts",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("discord_id", sa.String(32), nullable=False, unique=True),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE public.discord_accounts ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON TABLE public.discord_accounts FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(f"""
                DO $$ BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        REVOKE ALL ON TABLE public.discord_accounts FROM {role};
                    END IF;
                END $$
            """)


def downgrade():
    raise RuntimeError("Preserve Discord links through a reviewed forward migration.")
