"""Add manual payments; existing appointments retain their access.

Revision ID: c1a82b7d43ef
Revises: 9e45f7e7aa32
"""
from alembic import op
import sqlalchemy as sa

revision = 'c1a82b7d43ef'
down_revision = '9e45f7e7aa32'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('appointments', sa.Column('payment_required', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('appointment_id', sa.Integer(), sa.ForeignKey('appointments.id'), nullable=False, unique=True),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('reference', sa.String(40), nullable=False, unique=True),
        sa.Column('method', sa.String(30)),
        sa.Column('receipt_path', sa.String(255)),
        sa.Column('transaction_reference', sa.String(120)),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('reviewed_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('reviewed_at', sa.DateTime()),
        sa.Column('submitted_at', sa.DateTime()),
        sa.Column('rejection_reason', sa.String(500)),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('amount >= 0', name='ck_payment_amount'),
        sa.CheckConstraint("status IN ('PENDING','SUBMITTED','VERIFIED','REJECTED')", name='ck_payment_status'),
    )
    op.create_index('ix_payments_student_id', 'payments', ['student_id'])
    op.create_index('ix_payments_status', 'payments', ['status'])


def downgrade():
    # Intentionally prevent deleting payment/financial history through rollback.
    raise RuntimeError('Payment history must be retained. Use a reviewed forward migration.')
