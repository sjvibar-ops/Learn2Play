"""Deny browser API roles access to backend-only XP and attendance audit tables.

Revision ID: b9d3e7f02c84
Revises: a8f2b6c01d73
Review the actual backend SQL role before applying this locally or remotely.
"""
from alembic import op

revision = 'b9d3e7f02c84'
down_revision = 'a8f2b6c01d73'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    for table in ('xp_events', 'attendance_events'):
        op.execute(f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'REVOKE ALL ON TABLE public.{table} FROM PUBLIC')
        op.execute(f'REVOKE ALL ON SEQUENCE public.{table}_id_seq FROM PUBLIC')
        # Standalone PostgreSQL installations do not have Supabase API roles.
        for role in ('anon', 'authenticated'):
            op.execute(f"""DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE ALL ON TABLE public.{table} FROM {role};
                    REVOKE ALL ON SEQUENCE public.{table}_id_seq FROM {role};
                END IF;
            END $$""")


def downgrade():
    raise RuntimeError('Do not reopen financial/progress API access through downgrade. Review a forward migration.')
