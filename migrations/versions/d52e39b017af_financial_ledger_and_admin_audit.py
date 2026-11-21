"""Add empty financial journal and administrator audit tables.

PostgreSQL enforces the journal posting boundary. SQLite can persist audit
events, but its migrated schema rejects journal transaction inserts.
"""

import sqlalchemy as sa
from alembic import op

revision = "d52e39b017af"
down_revision = "a6d2f9c83b10"
branch_labels = None
depends_on = None


PROTECTED_TABLES = ("ledger_accounts", "ledger_transactions", "ledger_lines", "admin_audit_events")

POSTGRES_STATEMENTS = (
    """
    CREATE FUNCTION public.prevent_financial_history_mutation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        RAISE EXCEPTION 'Financial and administrator history is append-only'
            USING ERRCODE = '23514';
    END;
    $$
    """,
    """
    CREATE FUNCTION public.guard_ledger_transaction() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE
        line_count bigint;
        line_total numeric;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.sealed THEN
                RAISE EXCEPTION 'A ledger transaction must be staged before sealing' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END IF;

        IF OLD.sealed OR NOT NEW.sealed OR
            (to_jsonb(OLD) - 'sealed') IS DISTINCT FROM (to_jsonb(NEW) - 'sealed') THEN
            RAISE EXCEPTION 'Ledger transactions can only be sealed once' USING ERRCODE = '23514';
        END IF;

        SELECT count(*), sum(centavos) INTO line_count, line_total
        FROM public.ledger_lines WHERE transaction_id = NEW.id;
        IF line_count < 2 OR line_total <> 0 THEN
            RAISE EXCEPTION 'A ledger transaction requires at least two lines and a zero total'
                USING ERRCODE = '23514';
        END IF;

        IF NEW.payment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.payments p
            WHERE p.id = NEW.payment_id AND p.appointment_id = NEW.appointment_id
        ) OR NEW.appointment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.appointments a
            WHERE a.id = NEW.appointment_id AND a.currency = NEW.currency
              AND (NEW.provider_id IS NULL OR a.provider_id = NEW.provider_id)
        ) THEN
            RAISE EXCEPTION 'Invalid ledger payment, appointment or provider association' USING ERRCODE = '23514';
        END IF;

        IF NEW.payment_event_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.payment_events e
            WHERE e.id = NEW.payment_event_id AND e.payment_id = NEW.payment_id
              AND e.appointment_id = NEW.appointment_id
              AND (NEW.kind <> 'allocation' OR e.event_type = 'VERIFIED')
        ) THEN
            RAISE EXCEPTION 'Invalid ledger payment event association' USING ERRCODE = '23514';
        END IF;

        IF NEW.original_transaction_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.ledger_transactions original
            WHERE original.id = NEW.original_transaction_id AND original.sealed
              AND original.currency = NEW.currency
              AND original.payment_id IS NOT DISTINCT FROM NEW.payment_id
              AND original.appointment_id IS NOT DISTINCT FROM NEW.appointment_id
              AND original.provider_id IS NOT DISTINCT FROM NEW.provider_id
        ) THEN
            RAISE EXCEPTION 'Invalid original ledger transaction' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE FUNCTION public.require_sealed_ledger_transaction() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM public.ledger_transactions WHERE id = NEW.id AND sealed
        ) THEN
            RAISE EXCEPTION 'Unsealed ledger transactions cannot commit' USING ERRCODE = '23514';
        END IF;
        RETURN NULL;
    END;
    $$
    """,
    """
    CREATE FUNCTION public.guard_ledger_line_insert() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE
        journal record;
        account_owner integer;
    BEGIN
        SELECT * INTO journal FROM public.ledger_transactions WHERE id = NEW.transaction_id FOR UPDATE;
        IF NOT FOUND OR journal.sealed THEN
            RAISE EXCEPTION 'Ledger lines require an open transaction' USING ERRCODE = '23514';
        END IF;
        SELECT provider_id INTO account_owner FROM public.ledger_accounts WHERE id = NEW.account_id;
        IF account_owner IS NOT NULL AND account_owner IS DISTINCT FROM journal.provider_id THEN
            RAISE EXCEPTION 'Ledger account belongs to a different provider' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE FUNCTION public.seal_ledger_transaction(journal_id bigint) RETURNS void
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        UPDATE public.ledger_transactions SET sealed = true WHERE id = journal_id AND NOT sealed;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Ledger transaction is missing or already sealed' USING ERRCODE = '23514';
        END IF;
    END;
    $$
    """,
    """CREATE TRIGGER ledger_transaction_seal_guard BEFORE INSERT OR UPDATE ON public.ledger_transactions
       FOR EACH ROW EXECUTE FUNCTION public.guard_ledger_transaction()""",
    """CREATE CONSTRAINT TRIGGER ledger_transaction_requires_seal AFTER INSERT ON public.ledger_transactions
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_sealed_ledger_transaction()""",
    """CREATE TRIGGER ledger_line_insert_guard BEFORE INSERT ON public.ledger_lines
       FOR EACH ROW EXECUTE FUNCTION public.guard_ledger_line_insert()""",
    *(f"""CREATE TRIGGER {table}_no_mutation BEFORE {operations} ON public.{table}
         FOR EACH ROW EXECUTE FUNCTION public.prevent_financial_history_mutation()"""
      for table, operations in (
          ("ledger_transactions", "DELETE"), ("ledger_accounts", "UPDATE OR DELETE"),
          ("ledger_lines", "UPDATE OR DELETE"), ("admin_audit_events", "UPDATE OR DELETE"),
      )),
    *(f"""CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON public.{table}
         FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_financial_history_mutation()"""
      for table in PROTECTED_TABLES),
)

SQLITE_STATEMENTS = (
    """CREATE TRIGGER ledger_transaction_requires_postgres BEFORE INSERT ON ledger_transactions
       BEGIN SELECT RAISE(ABORT, 'Ledger posting requires PostgreSQL'); END""",
    *(f"""CREATE TRIGGER admin_audit_events_no_{operation.lower()} BEFORE {operation} ON admin_audit_events
         BEGIN SELECT RAISE(ABORT, 'Administrator audit events are append-only'); END"""
      for operation in ("UPDATE", "DELETE")),
)


def postgres_access_statements():
    """Use the same backend-only access policy as existing payment history."""
    for table in PROTECTED_TABLES:
        yield f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"
        yield f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC"
        if table != "ledger_lines":
            yield f"REVOKE ALL ON SEQUENCE public.{table}_id_seq FROM PUBLIC"
        for role in ("anon", "authenticated"):
            sequence = "" if table == "ledger_lines" else (
                f"REVOKE ALL ON SEQUENCE public.{table}_id_seq FROM {role};"
            )
            yield f"""
                DO $$ BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        REVOKE ALL ON TABLE public.{table} FROM {role};
                        {sequence}
                    END IF;
                END $$
            """
    yield "REVOKE EXECUTE ON FUNCTION public.seal_ledger_transaction(bigint) FROM PUBLIC"
    for role in ("anon", "authenticated"):
        yield f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE EXECUTE ON FUNCTION public.seal_ledger_transaction(bigint) FROM {role};
                END IF;
            END $$
        """


def _id():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _reference(column, target, type_=sa.Integer, **kwargs):
    return sa.Column(column, type_, sa.ForeignKey(target, ondelete="RESTRICT"), **kwargs)


def upgrade():
    op.create_table(
        "admin_audit_events",
        sa.Column("id", _id(), primary_key=True),
        _reference("actor_id", "users.id", nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("reason", sa.String(40)),
        sa.Column("before", sa.JSON(none_as_null=True)),
        sa.Column("after", sa.JSON(none_as_null=True)),
        sa.Column("correlation_id", sa.Uuid),
        _reference("related_event_id", "admin_audit_events.id", sa.BigInteger),
        sa.CheckConstraint("length(trim(action)) > 0 AND length(trim(target_type)) > 0 AND target_id > 0",
                           name="ck_admin_audit_target"),
    )
    op.create_table(
        "ledger_accounts",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        _reference("provider_id", "users.id"),
        sa.CheckConstraint("currency = 'PHP'", name="ck_ledger_account_currency"),
        sa.CheckConstraint(
            "(kind IN ('payment_clearing', 'platform_commission') AND provider_id IS NULL) OR "
            "(kind IN ('provider_pending', 'provider_available', 'provider_held', 'provider_settlement') "
            "AND provider_id IS NOT NULL)",
            name="ck_ledger_account_owner",
        ),
        sa.UniqueConstraint("id", "currency", name="uq_ledger_account_currency"),
        sa.UniqueConstraint("provider_id", "kind", "currency", name="uq_ledger_provider_account"),
    )
    op.create_table(
        "ledger_transactions",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        _reference("payment_id", "payments.id"),
        _reference("appointment_id", "appointments.id"),
        _reference("provider_id", "users.id"),
        _reference("payment_event_id", "payment_events.id"),
        _reference("admin_audit_id", "admin_audit_events.id", sa.BigInteger),
        _reference("actor_id", "users.id"),
        sa.Column("system_actor", sa.String(40)),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        _reference("original_transaction_id", "ledger_transactions.id", sa.BigInteger),
        sa.Column("sealed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint("currency = 'PHP'", name="ck_ledger_transaction_currency"),
        sa.CheckConstraint("kind IN ('allocation', 'release', 'hold', 'refund', 'adjustment', 'reversal', 'settlement')",
                           name="ck_ledger_transaction_type"),
        sa.CheckConstraint("(payment_event_id IS NULL) <> (admin_audit_id IS NULL)", name="ck_ledger_source"),
        sa.CheckConstraint("(actor_id IS NOT NULL AND system_actor IS NULL) OR "
                           "(actor_id IS NULL AND system_actor IS NOT NULL AND length(trim(system_actor)) > 0)",
                           name="ck_ledger_actor"),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_ledger_reason"),
        sa.CheckConstraint("kind <> 'allocation' OR "
                           "(payment_id IS NOT NULL AND appointment_id IS NOT NULL AND provider_id IS NOT NULL "
                           "AND payment_event_id IS NOT NULL AND original_transaction_id IS NULL)",
                           name="ck_ledger_allocation_source"),
        sa.CheckConstraint("kind NOT IN ('refund', 'adjustment', 'reversal') OR "
                           "(admin_audit_id IS NOT NULL AND original_transaction_id IS NOT NULL)",
                           name="ck_ledger_compensation_source"),
        sa.UniqueConstraint("id", "currency", name="uq_ledger_transaction_currency"),
        sa.UniqueConstraint("kind", "payment_event_id", name="uq_ledger_payment_event_kind"),
        sa.UniqueConstraint("admin_audit_id", name="uq_ledger_admin_decision"),
    )
    op.create_table(
        "ledger_lines",
        sa.Column("transaction_id", sa.BigInteger, primary_key=True),
        sa.Column("ordinal", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("centavos", sa.BigInteger, nullable=False),
        sa.ForeignKeyConstraint(["transaction_id", "currency"], ["ledger_transactions.id", "ledger_transactions.currency"],
                                ondelete="RESTRICT", name="fk_ledger_line_transaction_currency"),
        sa.ForeignKeyConstraint(["account_id", "currency"], ["ledger_accounts.id", "ledger_accounts.currency"],
                                ondelete="RESTRICT", name="fk_ledger_line_account_currency"),
        sa.CheckConstraint("ordinal > 0 AND centavos <> 0", name="ck_ledger_line_values"),
    )
    for name, table, columns in (
        ("ix_admin_audit_target", "admin_audit_events", ["target_type", "target_id", "id"]),
        ("ix_admin_audit_actor", "admin_audit_events", ["actor_id", "id"]),
        ("ix_admin_audit_correlation", "admin_audit_events", ["correlation_id"]),
        ("ix_ledger_payment", "ledger_transactions", ["payment_id", "id"]),
        ("ix_ledger_provider", "ledger_transactions", ["provider_id", "id"]),
        ("ix_ledger_original", "ledger_transactions", ["original_transaction_id"]),
        ("ix_ledger_line_account", "ledger_lines", ["account_id", "transaction_id"]),
    ):
        op.create_index(name, table, columns)
    for name, table, columns, predicate in (
        ("uq_ledger_platform_account", "ledger_accounts", ["kind", "currency"], "provider_id IS NULL"),
        ("uq_ledger_initial_allocation", "ledger_transactions", ["payment_id"], "kind = 'allocation'"),
    ):
        op.create_index(name, table, columns, unique=True, postgresql_where=sa.text(predicate),
                        sqlite_where=sa.text(predicate))

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for statement in SQLITE_STATEMENTS:
            op.execute(statement)
    elif dialect == "postgresql":
        for statement in (*POSTGRES_STATEMENTS, *postgres_access_statements()):
            op.execute(statement)
    else:
        raise RuntimeError("Financial foundation supports PostgreSQL and SQLite only.")


def downgrade():
    raise RuntimeError("Financial and administrator history must be retained. Use a reviewed forward migration.")
