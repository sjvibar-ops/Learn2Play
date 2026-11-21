"""Append-only payment submissions/reviews; no historical data is invented.

Revision ID: e9b7c2d41a60
Revises: c7e4a1b92d60
"""
from alembic import op
import sqlalchemy as sa

revision = 'e9b7c2d41a60'
down_revision = 'c7e4a1b92d60'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('payments') as batch:
        batch.add_column(sa.Column('submission_revision', sa.Integer(), nullable=False, server_default='0'))
        batch.create_check_constraint('ck_payment_submission_revision', 'submission_revision >= 0')
    op.create_table('payment_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('payment_id', sa.Integer(), sa.ForeignKey('payments.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('appointment_id', sa.Integer(), sa.ForeignKey('appointments.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('reviewer_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('event_type', sa.String(30), nullable=False),
        sa.Column('previous_status', sa.String(20), nullable=False),
        sa.Column('new_status', sa.String(20), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('receipt_path', sa.String(255)),
        sa.Column('method', sa.String(30)),
        sa.Column('transaction_reference', sa.String(120)),
        sa.Column('rejection_reason', sa.String(500)),
        sa.Column('submitted_at', sa.DateTime()),
        sa.Column('reviewed_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('payment_id', 'revision', 'event_type', name='uq_payment_event_revision_type'),
        sa.CheckConstraint('revision >= 0', name='ck_payment_event_revision'),
        sa.CheckConstraint("event_type IN ('SUBMITTED','RESUBMITTED','REJECTED','VERIFIED','LEGACY_SNAPSHOT')", name='ck_payment_event_type'),
        sa.CheckConstraint("previous_status IN ('PENDING','SUBMITTED','REJECTED','VERIFIED') AND new_status IN ('PENDING','SUBMITTED','REJECTED','VERIFIED')", name='ck_payment_event_status'),
    )
    op.create_index('ix_payment_events_payment_id', 'payment_events', ['payment_id'])
    dialect = op.get_bind().dialect.name
    if dialect == 'sqlite':
        for operation in ('UPDATE', 'DELETE'):
            op.execute(f"CREATE TRIGGER payment_events_no_{operation.lower()} BEFORE {operation} ON payment_events BEGIN SELECT RAISE(ABORT, 'Payment audit events are append-only'); END")
    if dialect == 'postgresql':
        op.execute("""CREATE FUNCTION prevent_payment_event_mutation() RETURNS trigger LANGUAGE plpgsql
            AS $$ BEGIN RAISE EXCEPTION 'Payment audit events are append-only'; END; $$""")
        op.execute("""CREATE TRIGGER payment_events_no_mutation BEFORE UPDATE OR DELETE ON payment_events
            FOR EACH ROW EXECUTE FUNCTION prevent_payment_event_mutation()""")
        op.execute("""CREATE TRIGGER payment_events_no_truncate BEFORE TRUNCATE ON payment_events
            FOR EACH STATEMENT EXECUTE FUNCTION prevent_payment_event_mutation()""")
        op.execute('ALTER TABLE public.payment_events ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL ON TABLE public.payment_events FROM PUBLIC')
        op.execute('REVOKE ALL ON SEQUENCE public.payment_events_id_seq FROM PUBLIC')
        for role in ('anon', 'authenticated'):
            op.execute(f"""DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE ALL ON TABLE public.payment_events FROM {role};
                    REVOKE ALL ON SEQUENCE public.payment_events_id_seq FROM {role};
                END IF;
            END $$""")


def downgrade():
    raise RuntimeError('Payment audit history cannot be removed through downgrade. Review a forward migration and retention policy.')
