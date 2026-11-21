"""Restore transfer indexes lost during the SQLite payment-table rebuild."""

from alembic import op
from sqlalchemy import text


revision = "db71a6e5c902"
down_revision = "c83d9f2a71e4"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return

    conflict = connection.execute(text("""
        SELECT 1 FROM payments
        WHERE status = 'VERIFIED' AND method IS NOT NULL
          AND transaction_reference IS NOT NULL
        GROUP BY lower(trim(method)), lower(trim(transaction_reference))
        HAVING count(*) > 1 LIMIT 1
    """)).first()
    if conflict:
        raise RuntimeError("Duplicate verified transfers require manual reconciliation before restoring the indexes.")

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_verified_transfer
        ON payments (lower(trim(method)), lower(trim(transaction_reference)))
        WHERE status = 'VERIFIED'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_payments_transfer_claim
        ON payments (lower(trim(method)), lower(trim(transaction_reference)))
    """)


def downgrade():
    # Both indexes were already required by c7e4a1b92d60.
    pass
