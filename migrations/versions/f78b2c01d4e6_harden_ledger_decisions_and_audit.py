"""Bind compensation to an explicit decision and protect audit insertions.

PostgreSQL deployment contract: a separate schema owner may provision the
NOLOGIN learn2play_financial_runtime role before upgrade and grant membership
only to the trusted Flask login. The login must have its existing authorized
read access to payments, appointments and payment_events, including any
required policies there. It must not own the protected schema or tables or
inherit broader financial table grants. If the role is absent, this revision
adds no runtime grants. It never creates a login or changes existing roles.
"""

from alembic import op

revision = "f78b2c01d4e6"
down_revision = "d52e39b017af"
branch_labels = None
depends_on = None

RUNTIME_ROLE = "learn2play_financial_runtime"

POSTGRES_STATEMENTS = (
    """
    CREATE FUNCTION public.require_compensation_decision() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM public.admin_audit_events decision
            WHERE decision.id = NEW.admin_audit_id
              AND decision.action = CASE NEW.kind
                  WHEN 'refund' THEN 'refund_confirmed'
                  WHEN 'adjustment' THEN 'adjustment_approved'
                  WHEN 'reversal' THEN 'payment_corrected'
              END
              AND (
                  (decision.target_type = 'payment' AND decision.target_id = NEW.payment_id)
                  OR (decision.target_type = 'ledger_transaction'
                      AND decision.target_id = NEW.original_transaction_id)
              )
        ) THEN
            RAISE EXCEPTION 'Compensation requires a matching financial decision' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """CREATE TRIGGER ledger_transaction_compensation_decision
       BEFORE UPDATE OF sealed ON public.ledger_transactions FOR EACH ROW
       WHEN (NEW.sealed AND NEW.kind IN ('refund', 'adjustment', 'reversal'))
       EXECUTE FUNCTION public.require_compensation_decision()""",
    """
    CREATE FUNCTION public.require_related_audit_target() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM public.admin_audit_events related
            WHERE related.id = NEW.related_event_id AND related.target_type = NEW.target_type
              AND related.target_id = NEW.target_id AND related.id <> NEW.id
        ) THEN
            RAISE EXCEPTION 'Related audit event must have the same target' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """CREATE TRIGGER admin_audit_related_target BEFORE INSERT ON public.admin_audit_events
       FOR EACH ROW WHEN (NEW.related_event_id IS NOT NULL)
       EXECUTE FUNCTION public.require_related_audit_target()""",
)

SQLITE_STATEMENTS = (
    """CREATE TRIGGER admin_audit_no_replacement BEFORE INSERT ON admin_audit_events
       WHEN EXISTS (SELECT 1 FROM admin_audit_events WHERE id = NEW.id)
       BEGIN SELECT RAISE(ABORT, 'Administrator audit events cannot be replaced'); END""",
    """CREATE TRIGGER admin_audit_related_target BEFORE INSERT ON admin_audit_events
       WHEN NEW.related_event_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM admin_audit_events related
           WHERE related.id = NEW.related_event_id AND related.target_type = NEW.target_type
             AND related.target_id = NEW.target_id AND related.id <> NEW.id
       )
       BEGIN SELECT RAISE(ABORT, 'Related audit event must have the same target'); END""",
)


def postgres_runtime_statements():
    """Frozen grants for an externally provisioned, trusted runtime role."""
    for table in ("ledger_accounts", "ledger_transactions", "ledger_lines", "admin_audit_events"):
        yield f"REVOKE ALL ON TABLE public.{table} FROM {RUNTIME_ROLE}"
        yield f"GRANT SELECT, INSERT ON TABLE public.{table} TO {RUNTIME_ROLE}"
        yield f"CREATE POLICY financial_runtime_read ON public.{table} FOR SELECT TO {RUNTIME_ROLE} USING (true)"
        yield f"CREATE POLICY financial_runtime_insert ON public.{table} FOR INSERT TO {RUNTIME_ROLE} WITH CHECK (true)"
        if table != "ledger_lines":
            yield f"REVOKE ALL ON SEQUENCE public.{table}_id_seq FROM {RUNTIME_ROLE}"
            yield f"GRANT USAGE ON SEQUENCE public.{table}_id_seq TO {RUNTIME_ROLE}"
    yield f"GRANT UPDATE (sealed) ON public.ledger_transactions TO {RUNTIME_ROLE}"
    yield f"""CREATE POLICY financial_runtime_seal ON public.ledger_transactions
              FOR UPDATE TO {RUNTIME_ROLE} USING (true) WITH CHECK (true)"""
    yield f"GRANT EXECUTE ON FUNCTION public.seal_ledger_transaction(bigint) TO {RUNTIME_ROLE}"


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for statement in POSTGRES_STATEMENTS:
            op.execute(statement)
        statements = ";\n".join(postgres_runtime_statements())
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
                    {statements};
                END IF;
            END $$
        """)
    elif dialect == "sqlite":
        for statement in SQLITE_STATEMENTS:
            op.execute(statement)
    else:
        raise RuntimeError("Financial audit protections support PostgreSQL and SQLite only.")


def downgrade():
    raise RuntimeError("Financial and administrator history must be retained. Use a reviewed forward migration.")
