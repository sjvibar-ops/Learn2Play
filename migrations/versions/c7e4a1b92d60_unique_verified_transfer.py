"""Reserve each method/reference identity only when a payment is VERIFIED.

Revision ID: c7e4a1b92d60
Revises: b9d3e7f02c84
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7e4a1b92d60'
down_revision = 'b9d3e7f02c84'
branch_labels = None
depends_on = None


def upgrade():
    # Do not choose a winner, delete history, or silently change existing status.
    conflict = op.get_bind().execute(sa.text("""
        SELECT 1 FROM payments
        WHERE status = 'VERIFIED' AND method IS NOT NULL
          AND transaction_reference IS NOT NULL
        GROUP BY lower(trim(method)), lower(trim(transaction_reference))
        HAVING count(*) > 1 LIMIT 1
    """)).first()
    if conflict:
        raise RuntimeError('Existing duplicate VERIFIED transfer allocations require manual reconciliation before this migration. No payment records were changed.')
    op.create_index('ix_payments_transfer_claim', 'payments',
                    [sa.text('lower(trim(method))'), sa.text('lower(trim(transaction_reference))')])
    op.create_index('uq_payments_verified_transfer', 'payments',
                    [sa.text('lower(trim(method))'), sa.text('lower(trim(transaction_reference))')],
                    unique=True, postgresql_where=sa.text("status = 'VERIFIED'"),
                    sqlite_where=sa.text("status = 'VERIFIED'"))


def downgrade():
    raise RuntimeError('Do not remove verified-transfer allocation protection through downgrade. Review a forward migration.')
