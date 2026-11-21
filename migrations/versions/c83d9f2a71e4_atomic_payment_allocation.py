"""Require an exact allocation for each new PostgreSQL payment verification.

Pause approvals and drain old writers before upgrade. Apply as the migration
owner. The trusted Flask login needs its existing source-table read/write
grants and membership in learn2play_financial_runtime. Verify these under
separate logins before explicitly enabling PAYMENT_LEDGER_APPROVAL_ENABLED.
"""

import sqlalchemy as sa
from alembic import op

revision = "c83d9f2a71e4"
down_revision = "f78b2c01d4e6"
branch_labels = None
depends_on = None


POSTGRES_STATEMENTS = (
    """
    CREATE FUNCTION public.phase2_payment_marker() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.status = 'VERIFIED' OR NEW.ledger_verification_xid IS NOT NULL THEN
                RAISE EXCEPTION 'Verification must transition from submitted' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.ledger_verification_xid IS DISTINCT FROM OLD.ledger_verification_xid THEN
            RAISE EXCEPTION 'Verification marker is database managed' USING ERRCODE = '23514';
        END IF;
        IF OLD.status = 'VERIFIED' THEN
            IF NEW.status <> 'VERIFIED' OR (OLD.ledger_verification_xid IS NOT NULL AND
                ROW(NEW.id, NEW.appointment_id, NEW.student_id, NEW.amount, NEW.reference,
                    NEW.method, NEW.transaction_reference, NEW.receipt_path, NEW.submission_revision,
                    NEW.submitted_at, NEW.reviewed_by, NEW.reviewed_at)
                IS DISTINCT FROM
                ROW(OLD.id, OLD.appointment_id, OLD.student_id, OLD.amount, OLD.reference,
                    OLD.method, OLD.transaction_reference, OLD.receipt_path, OLD.submission_revision,
                    OLD.submitted_at, OLD.reviewed_by, OLD.reviewed_at)) THEN
                RAISE EXCEPTION 'Verified payment evidence is immutable' USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.status = 'VERIFIED' THEN
            IF OLD.status <> 'SUBMITTED' OR OLD.ledger_verification_xid IS NOT NULL
                OR NEW.reviewed_by IS NULL OR NEW.reviewed_at IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM public.users reviewer
                    WHERE reviewer.id = NEW.reviewed_by AND reviewer.is_admin AND reviewer.is_active
                )
                OR NOT EXISTS (
                    SELECT 1 FROM public.appointments a
                    WHERE a.id = NEW.appointment_id AND a.status IN ('pending', 'accepted', 'scheduled')
                    FOR SHARE
                )
                OR ROW(NEW.id, NEW.appointment_id, NEW.student_id, NEW.amount, NEW.reference,
                       NEW.method, NEW.transaction_reference, NEW.receipt_path, NEW.submission_revision,
                       NEW.submitted_at)
                   IS DISTINCT FROM
                   ROW(OLD.id, OLD.appointment_id, OLD.student_id, OLD.amount, OLD.reference,
                       OLD.method, OLD.transaction_reference, OLD.receipt_path, OLD.submission_revision,
                       OLD.submitted_at)
                OR EXISTS (
                    SELECT 1 FROM public.payment_events e
                    WHERE e.payment_id = OLD.id AND e.new_status = 'VERIFIED'
                ) THEN
                RAISE EXCEPTION 'Ineligible payment verification transition' USING ERRCODE = '23514';
            END IF;
            NEW.ledger_verification_xid := CAST(pg_current_xact_id() AS text);
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """CREATE TRIGGER phase2_payment_marker BEFORE INSERT OR UPDATE ON public.payments
       FOR EACH ROW EXECUTE FUNCTION public.phase2_payment_marker()""",
    """
    CREATE FUNCTION public.phase2_appointment_scope() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        RAISE EXCEPTION 'Appointment financial scope is immutable' USING ERRCODE = '23514';
    END;
    $$
    """,
    """CREATE TRIGGER phase2_appointment_scope BEFORE UPDATE OF id, student_id, provider_id, currency, payment_required
       ON public.appointments FOR EACH ROW
       WHEN (ROW(NEW.id, NEW.student_id, NEW.provider_id, NEW.currency, NEW.payment_required) IS DISTINCT FROM
             ROW(OLD.id, OLD.student_id, OLD.provider_id, OLD.currency, OLD.payment_required))
       EXECUTE FUNCTION public.phase2_appointment_scope()""",
    """
    CREATE FUNCTION public.phase2_allocation_complete(payment_key integer, expected_revision integer)
    RETURNS boolean LANGUAGE sql SET search_path = pg_catalog AS $$
        SELECT EXISTS (
            SELECT 1
            FROM public.payments p
            JOIN public.appointments a ON a.id = p.appointment_id
            JOIN public.payment_events e ON e.payment_id = p.id AND e.appointment_id = a.id
            JOIN public.admin_audit_events audit ON audit.verification_event_id = e.id
            JOIN public.ledger_transactions j ON j.payment_event_id = e.id AND j.kind = 'allocation'
            CROSS JOIN LATERAL (
                SELECT p.amount * 100 AS gross, div(p.amount * 100 + 2, 5) AS commission
            ) split
            WHERE p.id = payment_key AND p.status = 'VERIFIED' AND p.ledger_verification_xid IS NOT NULL
              AND p.submission_revision = expected_revision
              AND a.currency = 'PHP' AND a.payment_required AND a.student_id = p.student_id
              AND a.provider_id <> a.student_id AND p.amount > 0
              AND p.method IN ('GCash', 'Maya', 'Bank transfer')
              AND length(trim(p.transaction_reference)) BETWEEN 4 AND 120
              AND length(trim(p.receipt_path)) > 0 AND p.submitted_at IS NOT NULL
              AND e.event_type = 'VERIFIED' AND e.previous_status = 'SUBMITTED' AND e.new_status = 'VERIFIED'
              AND e.revision = p.submission_revision AND e.student_id = p.student_id
              AND e.actor_id = p.reviewed_by AND e.reviewer_id = p.reviewed_by
              AND e.reviewed_at = p.reviewed_at AND e.submitted_at = p.submitted_at
              AND e.receipt_path = p.receipt_path AND e.method = p.method
              AND e.transaction_reference = p.transaction_reference
              AND audit.action = 'payment_approved' AND audit.reason = 'receiving_account_checked'
              AND audit.target_type = 'payment' AND audit.target_id = p.id AND audit.actor_id = p.reviewed_by
              AND j.sealed AND j.payment_id = p.id AND j.appointment_id = a.id
              AND j.provider_id = a.provider_id AND j.currency = a.currency AND j.actor_id = p.reviewed_by
              AND (
                  SELECT count(*) = CASE WHEN split.commission = 0 THEN 2 ELSE 3 END
                     AND count(*) FILTER (WHERE k.kind = 'payment_clearing' AND k.provider_id IS NULL
                                           AND l.centavos = split.gross) = 1
                     AND count(*) FILTER (WHERE k.kind = 'platform_commission' AND k.provider_id IS NULL
                                           AND l.centavos = -split.commission) =
                         CASE WHEN split.commission = 0 THEN 0 ELSE 1 END
                     AND count(*) FILTER (WHERE k.kind = 'provider_pending' AND k.provider_id = a.provider_id
                                           AND l.centavos = split.commission - split.gross) = 1
                  FROM public.ledger_lines l JOIN public.ledger_accounts k ON k.id = l.account_id
                  WHERE l.transaction_id = j.id AND l.currency = a.currency AND k.currency = a.currency
              )
        )
    $$
    """,
    """
    CREATE FUNCTION public.phase2_audit_link() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NEW.action <> 'payment_approved' OR NEW.reason IS DISTINCT FROM 'receiving_account_checked'
           OR NEW.target_type <> 'payment' OR NOT EXISTS (
               SELECT 1 FROM public.payment_events e JOIN public.payments p ON p.id = e.payment_id
               WHERE e.id = NEW.verification_event_id AND e.event_type = 'VERIFIED'
                 AND e.payment_id = NEW.target_id AND e.actor_id = NEW.actor_id
                 AND p.reviewed_by = NEW.actor_id AND p.status = 'VERIFIED'
                 AND p.ledger_verification_xid = CAST(pg_current_xact_id() AS text)
           ) THEN
            RAISE EXCEPTION 'Invalid verification audit link' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """CREATE TRIGGER phase2_audit_link BEFORE INSERT ON public.admin_audit_events
       FOR EACH ROW WHEN (NEW.verification_event_id IS NOT NULL)
       EXECUTE FUNCTION public.phase2_audit_link()""",
    """
    CREATE FUNCTION public.phase2_check_allocation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM public.payments p
            WHERE p.id = NEW.payment_id AND p.ledger_verification_xid = CAST(pg_current_xact_id() AS text)
              AND public.phase2_allocation_complete(p.id, p.submission_revision)
        ) THEN
            RAISE EXCEPTION 'Invalid initial payment allocation' USING ERRCODE = '23514';
        END IF;
        RETURN NULL;
    END;
    $$
    """,
    """CREATE TRIGGER phase2_check_allocation AFTER UPDATE OF sealed ON public.ledger_transactions
       FOR EACH ROW WHEN (NEW.sealed AND NEW.kind = 'allocation')
       EXECUTE FUNCTION public.phase2_check_allocation()""",
    """
    CREATE FUNCTION public.phase2_require_allocation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NOT public.phase2_allocation_complete(NEW.id, NEW.submission_revision) THEN
            RAISE EXCEPTION 'Payment verification requires its sealed allocation and audit' USING ERRCODE = '23514';
        END IF;
        RETURN NULL;
    END;
    $$
    """,
    """CREATE CONSTRAINT TRIGGER phase2_require_allocation AFTER UPDATE ON public.payments
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.status <> 'VERIFIED' AND NEW.status = 'VERIFIED')
       EXECUTE FUNCTION public.phase2_require_allocation()""",
    """
    CREATE FUNCTION public.phase2_approval_ready() RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
        SELECT current_setting('session_replication_role') = 'origin' AND (
            SELECT count(*) = 5 FROM pg_trigger
            WHERE tgenabled IN ('O', 'A') AND (tgrelid, tgname) IN (
                (CAST('public.payments' AS regclass), 'phase2_payment_marker'),
                (CAST('public.payments' AS regclass), 'phase2_require_allocation'),
                (CAST('public.appointments' AS regclass), 'phase2_appointment_scope'),
                (CAST('public.admin_audit_events' AS regclass), 'phase2_audit_link'),
                (CAST('public.ledger_transactions' AS regclass), 'phase2_check_allocation')
            )
        )
    $$
    """,
)


def upgrade():
    dialect = op.get_bind().dialect.name
    op.add_column("payments", sa.Column("ledger_verification_xid", sa.String(20)))
    if dialect == "sqlite":
        # Adding a nullable reference in place preserves the existing SQLite audit triggers.
        op.execute(
            "ALTER TABLE admin_audit_events ADD COLUMN verification_event_id "
            "INTEGER REFERENCES payment_events(id) ON DELETE RESTRICT"
        )
        op.create_index("uq_admin_audit_verification_event", "admin_audit_events", ["verification_event_id"], unique=True)
    elif dialect == "postgresql":
        op.add_column("admin_audit_events", sa.Column(
            "verification_event_id", sa.Integer, sa.ForeignKey("payment_events.id", ondelete="RESTRICT"),
        ))
        op.create_unique_constraint(
            "uq_admin_audit_verification_event", "admin_audit_events", ["verification_event_id"],
        )
        for statement in POSTGRES_STATEMENTS:
            op.execute(statement)
        for function in ("phase2_approval_ready()", "phase2_allocation_complete(integer, integer)"):
            op.execute(f"REVOKE EXECUTE ON FUNCTION public.{function} FROM PUBLIC")
            op.execute(f"""
                DO $$ BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'learn2play_financial_runtime') THEN
                        GRANT EXECUTE ON FUNCTION public.{function} TO learn2play_financial_runtime;
                    END IF;
                END $$
            """)
    else:
        raise RuntimeError("Payment allocation schema supports PostgreSQL and SQLite only.")


def downgrade():
    raise RuntimeError("Payment verification protections and financial history require a reviewed forward migration.")
