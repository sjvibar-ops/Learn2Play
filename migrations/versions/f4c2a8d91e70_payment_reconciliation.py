"""Separate booking-end reconciliation; preserve payment verification and history.

Revision ID: f4c2a8d91e70
Revises: e9b7c2d41a60
"""
from alembic import op
import sqlalchemy as sa

revision = 'f4c2a8d91e70'
down_revision = 'e9b7c2d41a60'
branch_labels = None
depends_on = None

STATES = "'NOT_REQUIRED','NEEDS_REVIEW','NO_FUNDS_RECEIVED','REFUND_PENDING','REFUNDED','RETAINED'"
EVENT_TYPES = "'SUBMITTED','RESUBMITTED','REJECTED','VERIFIED','LEGACY_SNAPSHOT','BOOKING_ENDED','NEEDS_REVIEW','NO_FUNDS_RECEIVED','REFUND_PENDING','REFUNDED','RETAINED'"


def upgrade():
    with op.batch_alter_table('appointments') as batch:
        batch.add_column(sa.Column('ended_at', sa.DateTime()))
        batch.add_column(sa.Column('ended_by_id', sa.Integer()))
        batch.create_foreign_key('fk_appointments_ended_by_users', 'users', ['ended_by_id'], ['id'])
        batch.add_column(sa.Column('ending_reason', sa.String(500)))
    with op.batch_alter_table('payments') as batch:
        batch.add_column(sa.Column('reconciliation_state', sa.String(30), nullable=False, server_default='NOT_REQUIRED'))
        batch.add_column(sa.Column('reconciliation_version', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('reconciliation_explanation', sa.String(500)))
        batch.add_column(sa.Column('reconciliation_updated_at', sa.DateTime()))
        batch.add_column(sa.Column('reconciliation_updated_by_id', sa.Integer()))
        batch.create_foreign_key('fk_payments_reconciliation_updated_by_users', 'users', ['reconciliation_updated_by_id'], ['id'])
        batch.create_check_constraint('ck_payment_reconciliation_state', f'reconciliation_state IN ({STATES})')
        batch.create_check_constraint('ck_payment_reconciliation_version', 'reconciliation_version >= 0')
    op.create_index('ix_payments_reconciliation_state', 'payments', ['reconciliation_state'])
    with op.batch_alter_table('payment_events') as batch:
        for name, kind in (
            ('reconciliation_previous_state', sa.String(30)), ('reconciliation_state', sa.String(30)),
            ('reconciliation_version', sa.Integer()), ('reconciliation_explanation', sa.String(500)),
            ('booking_status', sa.String(20)), ('booking_ended_at', sa.DateTime()),
            ('booking_ending_reason', sa.String(500)), ('decision_reason', sa.String(500)),
            ('confirmation_notes', sa.String(1000)), ('evidence_reference', sa.String(200)),
            ('authority_name', sa.String(200)), ('authority_capacity', sa.String(60)),
            ('funds_received_confirmed', sa.Boolean()), ('independent_check_confirmed', sa.Boolean()),
            ('authorized_refund_confirmed', sa.Boolean()), ('account_holder_refund_confirmed', sa.Boolean()),
            ('authorized_retention_confirmed', sa.Boolean()),
        ):
            batch.add_column(sa.Column(name, kind))
        batch.drop_constraint('ck_payment_event_type', type_='check')
        batch.create_check_constraint('ck_payment_event_type', f'event_type IN ({EVENT_TYPES})')
        batch.create_check_constraint('ck_payment_event_reconciliation_version', 'reconciliation_version IS NULL OR reconciliation_version > 0')
        batch.create_check_constraint('ck_payment_event_reconciliation_state', f'reconciliation_state IS NULL OR reconciliation_state IN ({STATES})')
        batch.create_unique_constraint('uq_payment_event_reconciliation_version', ['payment_id', 'reconciliation_version'])
    # SQLite batch table recreation does not preserve triggers automatically.
    # PostgreSQL ALTER TABLE keeps the prior mutation/truncate guards and RLS.
    if op.get_bind().dialect.name == 'sqlite':
        for operation in ('UPDATE', 'DELETE'):
            op.execute(f"CREATE TRIGGER IF NOT EXISTS payment_events_no_{operation.lower()} BEFORE {operation} ON payment_events BEGIN SELECT RAISE(ABORT, 'Payment audit events are append-only'); END")
    # Conservative queue initialization only. Do not manufacture historical
    # actors, ending times, decisions, refunds or audit events.
    op.execute("""UPDATE payments SET reconciliation_state = 'NEEDS_REVIEW',
        reconciliation_explanation = 'Legacy ended booking with payment evidence. Independent financial review is required; no refund is inferred.'
        WHERE appointment_id IN (SELECT id FROM appointments WHERE status IN ('cancelled','rejected'))
        AND (status <> 'PENDING' OR receipt_path IS NOT NULL OR submitted_at IS NOT NULL
             OR reviewed_at IS NOT NULL OR submission_revision > 0
             OR EXISTS (SELECT 1 FROM payment_events WHERE payment_events.payment_id = payments.id))""")


def downgrade():
    raise RuntimeError('Do not remove reconciliation records or financial history through downgrade. Review a forward migration.')
