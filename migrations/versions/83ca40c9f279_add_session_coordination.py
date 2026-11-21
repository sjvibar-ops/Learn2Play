"""Support audited Discord rooms and mutual rescheduling."""

import sqlalchemy as sa
from alembic import op

revision = "83ca40c9f279"
down_revision = "4f78ec3f41a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discord_rooms",
        sa.Column("appointment_id", sa.Integer(), sa.ForeignKey("appointments.id"), primary_key=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("student_discord_id", sa.String(32), nullable=False),
        sa.Column("provider_discord_id", sa.String(32), nullable=False),
        sa.Column("marker", sa.String(64), nullable=False, unique=True),
        sa.Column("channel_id", sa.String(32), unique=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.CheckConstraint("state IN ('provisioning','ready','failed','removed')", name="ck_discord_room_state"),
    )
    op.create_table(
        "discord_room_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("appointment_id", sa.Integer(), sa.ForeignKey("discord_rooms.appointment_id"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "reschedule_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("appointment_id", sa.Integer(), sa.ForeignKey("appointments.id"), nullable=False),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("previous_start", sa.DateTime(), nullable=False),
        sa.Column("proposed_start", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint("status IN ('pending','accepted','rejected','withdrawn')", name="ck_reschedule_status"),
    )
    op.create_index("ix_reschedule_proposals_appointment_id", "reschedule_proposals", ["appointment_id"])
    pending = sa.text("status = 'pending'")
    op.create_index("uq_pending_reschedule", "reschedule_proposals", ["appointment_id"],
                    unique=True, sqlite_where=pending, postgresql_where=pending)
    if op.get_bind().dialect.name == "postgresql":
        for table in ("discord_rooms", "discord_room_events", "reschedule_proposals"):
            op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
            for role in ("anon", "authenticated"):
                op.execute(f"""
                    DO $$ BEGIN
                        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                            REVOKE ALL ON TABLE public.{table} FROM {role};
                        END IF;
                    END $$
                """)


def downgrade():
    raise RuntimeError("Retain Discord and rescheduling history through a reviewed forward migration.")
