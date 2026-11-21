from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from importlib import import_module
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from __init__ import create_app
from admin_audit import AuditAction, AuditReason, AuditTarget, record_admin_event
from extensions import db
from financial_ledger import allocate_payment, post_transaction
from models import AdminAuditEvent, Appointment, LedgerAccount, LedgerTransaction, Payment, User, utcnow
from payments import record_event
import test_attendance_postgres


MIGRATION = import_module("migrations.versions.d52e39b017af_financial_ledger_and_admin_audit")
REMEDIATION = import_module("migrations.versions.f78b2c01d4e6_harden_ledger_decisions_and_audit")


@pytest.mark.parametrize(("amount", "expected"), [
    ("100.00", (2000, 8000)), ("123.47", (2469, 9878)),
    ("0.01", (0, 1)), ("0.02", (0, 2)), ("0.03", (1, 2)),
    ("0.07", (1, 6)), ("0.08", (2, 6)), ("1.2300", (25, 98)),
])
def test_exact_allocation(amount, expected):
    with localcontext() as context:
        context.prec = 2
        result = allocate_payment(Decimal(amount), "PHP")
    assert result == expected
    numerator, denominator = Decimal(amount).as_integer_ratio()
    assert sum(result) == numerator * 100 // denominator


@pytest.mark.parametrize("amount", [
    Decimal("0"), Decimal("-1"), Decimal("0.001"), Decimal("1.2301"),
    Decimal("NaN"), Decimal("Infinity"), Decimal("92233720368547758.08"), 1.25, "1.25", True,
])
def test_invalid_amount(amount):
    with pytest.raises(ValueError):
        allocate_payment(amount, "PHP")


def test_unsupported_currency_and_migration_head():
    with pytest.raises(ValueError):
        allocate_payment(Decimal("1.00"), "USD")
    graph = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    assert graph.get_heads() == ["db71a6e5c902"]
    assert graph.get_revision("db71a6e5c902").down_revision == "c83d9f2a71e4"
    assert graph.get_revision("c83d9f2a71e4").down_revision == REMEDIATION.revision
    assert graph.get_revision(REMEDIATION.revision).down_revision == MIGRATION.revision
    assert graph.get_revision(MIGRATION.revision).down_revision == "a6d2f9c83b10"


@pytest.fixture
def isolated_audit():
    app = create_app({
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    with app.app_context():
        db.create_all()
        for statement in (*MIGRATION.SQLITE_STATEMENTS, *REMEDIATION.SQLITE_STATEMENTS):
            db.session.connection().exec_driver_sql(statement)
        actor = User(display_name="Audit operator", email="audit@example.test", password_hash="fixture")
        db.session.add(actor)
        db.session.commit()
        yield actor.id
        db.session.remove()
        db.engine.dispose()


def audit_event(actor_id, **overrides):
    fields = {
        "actor": db.session.get(User, actor_id), "action": AuditAction.PAYMENT_APPROVED,
        "target_type": AuditTarget.PAYMENT, "target_id": 1,
    }
    return record_admin_event(**(fields | overrides))


def test_audit_snapshot_reason_and_caller_rollback(isolated_audit):
    before = {"payment_status": "SUBMITTED"}
    correlation_id = uuid4()
    item = audit_event(
        isolated_audit, before=before, after={"payment_status": "VERIFIED", "amount_centavos": 1234},
        reason=AuditReason.RECEIVING_ACCOUNT_CHECKED, correlation_id=correlation_id,
    )
    before["payment_status"] = "REJECTED"
    db.session.flush()
    db.session.refresh(item)
    assert item.actor_id == isolated_audit
    assert item.action == "payment_approved"
    assert item.reason == "receiving_account_checked"
    assert item.before == {"payment_status": "SUBMITTED"}
    assert item.after == {"payment_status": "VERIFIED", "amount_centavos": 1234}
    assert item.correlation_id == correlation_id
    db.session.rollback()
    assert db.session.query(AdminAuditEvent).count() == 0


@pytest.mark.parametrize("override", [
    {"actor": None}, {"actor": 1}, {"action": None}, {"reason": "arbitrary free text"},
    {"before": {"password": "fixture"}}, {"after": {"token": "fixture"}},
    {"after": {"receipt_bytes": "fixture"}}, {"after": {"account_number": "fixture"}},
    {"after": {"payment_status": {"key": "fixture"}}},
    {"after": {"currency": "fixture"}}, {"correlation_id": "arbitrary string"},
])
def test_audit_rejects_missing_identity_or_unapproved_data(isolated_audit, override):
    with pytest.raises(ValueError):
        audit_event(isolated_audit, **override)
    assert not db.session.new


def test_sqlite_audit_orm_and_database_guards(isolated_audit):
    item = audit_event(isolated_audit)
    db.session.commit()
    item_id = item.id
    item.action = "replacement"
    with pytest.raises(ValueError, match="append-only"):
        db.session.flush()
    db.session.rollback()
    for statement in (
        "UPDATE admin_audit_events SET action = 'replacement' WHERE id = :id",
        "DELETE FROM admin_audit_events WHERE id = :id",
    ):
        with pytest.raises(IntegrityError, match="append-only"):
            db.session.execute(text(statement), {"id": item_id})
        db.session.rollback()
    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        post_transaction(kind="allocation", currency="PHP", lines=[], reason="Fixture")


def test_sqlite_replacement_and_foreign_keys(isolated_audit):
    original = audit_event(isolated_audit)
    db.session.commit()
    original_id = original.id
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    db.session.execute(text("PRAGMA recursive_triggers = OFF"))
    with pytest.raises(IntegrityError, match="cannot be replaced"):
        db.session.execute(text("""
            INSERT OR REPLACE INTO admin_audit_events (id, actor_id, action, target_type, target_id, created_at)
            VALUES (:id, :actor, 'payment_rejected', 'payment', 1, CURRENT_TIMESTAMP)
        """), {"id": original_id, "actor": isolated_audit})
    db.session.rollback()
    assert db.session.get(AdminAuditEvent, original_id).action == "payment_approved"
    db.session.add(AdminAuditEvent(actor_id=isolated_audit + 999, action="payment_approved",
                                  target_type="payment", target_id=1))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.session.commit()
    db.session.rollback()
    assert db.session.query(AdminAuditEvent).count() == 1


def assert_related_audit_target(actor):
    original = audit_event(actor)
    db.session.flush()
    related = audit_event(actor, related_event_id=original.id)
    db.session.flush()
    assert related.related_event_id == original.id
    for override in ({"target_id": 2}, {"target_type": AuditTarget.USER}, {"related_event_id": 999999}):
        with pytest.raises(IntegrityError, match="same target"), db.session.begin_nested():
            audit_event(actor, **({"related_event_id": original.id} | override))
            db.session.flush()


def test_sqlite_related_audit_target(isolated_audit):
    assert_related_audit_target(isolated_audit)
    db.session.rollback()


@pytest.fixture(scope="module")
def postgres_journal():
    # The existing harness initializes a disposable, loopback-only cluster.
    harness = test_attendance_postgres.PostgresAttendanceConcurrencyTests
    try:
        harness.setUpClass()
    except test_attendance_postgres.unittest.SkipTest as exc:
        pytest.skip(str(exc))
    instance = harness()
    try:
        instance.setUp()
        with instance.app.app_context():
            # Exercise the actual trigger SQL against fresh model DDL. Do not
            # invoke Alembic or execute the migration upgrade function.
            connection = db.session.connection()
            connection.exec_driver_sql("CREATE ROLE anon")
            connection.exec_driver_sql("CREATE ROLE authenticated")
            connection.exec_driver_sql("CREATE ROLE ledger_test_schema_owner")
            connection.exec_driver_sql(f"CREATE ROLE {REMEDIATION.RUNTIME_ROLE}")
            connection.exec_driver_sql("CREATE ROLE ledger_test_flask")
            connection.exec_driver_sql(f"GRANT {REMEDIATION.RUNTIME_ROLE} TO ledger_test_flask")
            connection.exec_driver_sql("GRANT USAGE, CREATE ON SCHEMA public TO ledger_test_schema_owner")
            connection.exec_driver_sql(
                "GRANT SELECT ON public.payments, public.appointments, public.payment_events TO ledger_test_flask"
            )
            for table in MIGRATION.PROTECTED_TABLES:
                connection.exec_driver_sql(f"ALTER TABLE public.{table} OWNER TO ledger_test_schema_owner")
            connection.exec_driver_sql("SET LOCAL ROLE ledger_test_schema_owner")
            for statement in (
                *MIGRATION.POSTGRES_STATEMENTS, *MIGRATION.postgres_access_statements(),
                *REMEDIATION.POSTGRES_STATEMENTS, *REMEDIATION.postgres_runtime_statements(),
            ):
                connection.exec_driver_sql(statement)
            connection.exec_driver_sql("RESET ROLE")
            payment = Payment(
                amount=Decimal("1.00"), reference="ledger-fixture", status="VERIFIED",
                appointment_id=instance.aid, student_id=instance.student,
            )
            clearing = LedgerAccount(kind="payment_clearing", currency="PHP")
            pending = LedgerAccount(kind="provider_pending", currency="PHP", provider_id=instance.provider)
            db.session.add_all([payment, clearing, pending])
            db.session.flush()
            first = record_event(payment, "VERIFIED", "SUBMITTED", instance.student)
            payment.submission_revision = 1
            second = record_event(payment, "VERIFIED", "SUBMITTED", instance.student)
            admin = audit_event(instance.student, target_id=payment.id)
            db.session.flush()
            instance.payment_id = payment.id
            instance.sources = first.id, second.id
            instance.accounts = clearing.id, pending.id
            instance.audit_id = admin.id
            appointment = Appointment(
                student_id=instance.student, provider_id=instance.provider, topic="Compensation fixture",
                start_time=utcnow(),
            )
            db.session.add(appointment)
            db.session.flush()
            other_payment = Payment(
                amount=Decimal("1.00"), reference="compensation-fixture", status="VERIFIED",
                appointment_id=appointment.id, student_id=instance.student,
            )
            db.session.add(other_payment)
            db.session.flush()
            other_source = record_event(other_payment, "VERIFIED", "SUBMITTED", instance.student)
            db.session.flush()
            instance.compensation_scope = {
                "payment_id": other_payment.id, "appointment_id": appointment.id, "provider_id": instance.provider,
            }
            instance.compensation_source = other_source.id
            db.session.commit()
        yield instance
    finally:
        harness.tearDownClass()


def allocation(fixture, *, source_index=0, values=(80, -80)):
    return post_transaction(
        kind="allocation", currency="PHP", reason="Test posting",
        actor_id=fixture.student, payment_id=fixture.payment_id, appointment_id=fixture.aid,
        provider_id=fixture.provider, payment_event_id=fixture.sources[source_index],
        lines=list(zip(fixture.accounts, values)),
    )


@pytest.mark.parametrize("values", [(80, -79), (80,), (0, 0)])
def test_postgres_database_rejects_invalid_lines(postgres_journal, values):
    with postgres_journal.app.app_context():
        with pytest.raises(IntegrityError):
            allocation(postgres_journal, values=values)
        db.session.rollback()


def staged_header(fixture):
    header = LedgerTransaction(
        kind="allocation", currency="PHP", reason="Test staging", actor_id=fixture.student,
        payment_id=fixture.payment_id, appointment_id=fixture.aid,
        provider_id=fixture.provider, payment_event_id=fixture.sources[0],
    )
    db.session.add(header)
    db.session.flush()
    return header


def test_postgres_cannot_commit_an_unsealed_header(postgres_journal):
    with postgres_journal.app.app_context():
        staged_header(postgres_journal)
        with pytest.raises(IntegrityError, match="Unsealed"):
            db.session.commit()
        db.session.rollback()


def test_postgres_rejects_mixed_line_currency(postgres_journal):
    with postgres_journal.app.app_context():
        header = staged_header(postgres_journal)
        with pytest.raises(IntegrityError, match="fk_ledger_line_.*_currency"):
            db.session.execute(text(
                "INSERT INTO ledger_lines VALUES (:id, 1, :account, 'USD', 80)"
            ), {"id": header.id, "account": postgres_journal.accounts[0]})
        db.session.rollback()


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_postgres_browser_roles_have_no_ledger_or_audit_access(postgres_journal, role):
    with postgres_journal.app.app_context():
        for table in MIGRATION.PROTECTED_TABLES:
            assert db.session.execute(text(
                "SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:table)"
            ), {"table": f"public.{table}"}).scalar_one()
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not db.session.execute(text(
                    "SELECT has_table_privilege(:role, :table, :privilege)"
                ), {"role": role, "table": f"public.{table}", "privilege": privilege}).scalar_one()
            if table != "ledger_lines":
                assert not db.session.execute(text(
                    "SELECT has_sequence_privilege(:role, :sequence, 'USAGE')"
                ), {"role": role, "sequence": f"public.{table}_id_seq"}).scalar_one()
        assert not db.session.execute(text(
            "SELECT has_function_privilege(:role, 'public.seal_ledger_transaction(bigint)', 'EXECUTE')"
        ), {"role": role}).scalar_one()


def test_postgres_balanced_seal_idempotency_and_history_guards(postgres_journal):
    with postgres_journal.app.app_context():
        first = allocation(postgres_journal)
        assert first.sealed
        db.session.execute(text("SET CONSTRAINTS ledger_transaction_requires_seal IMMEDIATE"))
        for operation in (
            "UPDATE ledger_transactions SET reason = 'replacement' WHERE id = :id",
            "DELETE FROM ledger_transactions WHERE id = :id",
            "UPDATE ledger_lines SET centavos = centavos + 1 WHERE transaction_id = :id",
            "DELETE FROM ledger_lines WHERE transaction_id = :id",
            "INSERT INTO ledger_lines VALUES (:id, 9, :account, 'PHP', 1)",
            "UPDATE ledger_accounts SET kind = 'platform_commission' WHERE id = :account",
            "TRUNCATE ledger_transactions CASCADE",
            "UPDATE admin_audit_events SET action = 'replacement' WHERE id = :audit",
            "DELETE FROM admin_audit_events WHERE id = :audit",
            "TRUNCATE admin_audit_events CASCADE",
        ):
            with pytest.raises(IntegrityError), db.session.begin_nested():
                db.session.execute(text(operation), {
                    "id": first.id, "account": postgres_journal.accounts[0], "audit": postgres_journal.audit_id,
                })
        for source_index in (0, 1):
            with pytest.raises(IntegrityError), db.session.begin_nested():
                allocation(postgres_journal, source_index=source_index)
        db.session.rollback()


def test_postgres_concurrent_duplicate_allocation(postgres_journal):
    barrier = Barrier(2, timeout=10)

    def post():
        with postgres_journal.app.app_context():
            barrier.wait()
            try:
                allocation(postgres_journal)
                db.session.commit()
            except IntegrityError as exc:
                assert exc.orig.sqlstate == "23505"
                db.session.rollback()
                return "duplicate"
            return "posted"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: post(), range(2))) == ["duplicate", "posted"]
    with postgres_journal.app.app_context():
        assert db.session.query(LedgerTransaction).count() == 1


def test_postgres_compensation_provenance_as_runtime(postgres_journal):
    fixture = postgres_journal
    with fixture.app.app_context():
        actor = db.session.get(User, fixture.student)
        db.session.execute(text("SET LOCAL ROLE ledger_test_flask"))
        scope = fixture.compensation_scope
        original = post_transaction(
            kind="allocation", currency="PHP", reason="Initial fixture", actor_id=actor.id,
            payment_event_id=fixture.compensation_source, lines=list(zip(fixture.accounts, (100, -100))),
            **scope,
        )

        def decision(action, target_type, target_id):
            event = record_admin_event(actor=actor, action=action, target_type=target_type, target_id=target_id)
            db.session.flush()
            return event.id

        def compensate(source, **overrides):
            return post_transaction(**({
                **scope, "kind": "adjustment", "currency": "PHP", "reason": "Adjustment fixture",
                "actor_id": actor.id, "admin_audit_id": source, "original_transaction_id": original.id,
                "lines": list(zip(fixture.accounts, (-30, 30))),
            } | overrides))

        for action, target_type, target_id in (
            (AuditAction.ADMIN_PRIVILEGE_CHANGED, AuditTarget.USER, actor.id),
            (AuditAction.ADJUSTMENT_APPROVED, AuditTarget.PAYMENT, fixture.payment_id),
            (AuditAction.REFUND_CONFIRMED, AuditTarget.PAYMENT, scope["payment_id"]),
        ):
            source = decision(action, target_type, target_id)
            with pytest.raises(IntegrityError, match="matching financial decision"), db.session.begin_nested():
                compensate(source)

        with pytest.raises(IntegrityError, match="same target"), db.session.begin_nested():
            record_admin_event(actor=actor, action=AuditAction.ADJUSTMENT_APPROVED,
                               target_type=AuditTarget.PAYMENT, target_id=scope["payment_id"],
                               related_event_id=fixture.audit_id)
            db.session.flush()
        source = decision(AuditAction.ADJUSTMENT_APPROVED, AuditTarget.LEDGER_TRANSACTION, original.id)
        with pytest.raises(IntegrityError, match="different provider"), db.session.begin_nested():
            compensate(source, provider_id=fixture.student)
        with pytest.raises(IntegrityError, match="Invalid ledger payment"), db.session.begin_nested():
            compensate(source, payment_id=fixture.payment_id)
        posted = compensate(source)
        assert posted.sealed and original.sealed
        with pytest.raises(IntegrityError, match="uq_ledger_admin_decision"), db.session.begin_nested():
            compensate(source, lines=list(zip(fixture.accounts, (-40, 40))))
        payment_id = scope["payment_id"]
        db.session.rollback()
        assert not db.session.query(LedgerTransaction).filter_by(payment_id=payment_id).count()


def test_postgres_runtime_grants_and_direct_seal_guard(postgres_journal):
    fixture = postgres_journal
    with fixture.app.app_context():
        db.session.execute(text("SET LOCAL ROLE ledger_test_flask"))
        for operation in (
            "UPDATE ledger_transactions SET reason = 'replacement'",
            "DELETE FROM ledger_transactions",
            "TRUNCATE ledger_transactions",
            "UPDATE ledger_lines SET centavos = 1",
            "DELETE FROM admin_audit_events",
            "TRUNCATE admin_audit_events",
        ):
            with pytest.raises(DBAPIError) as failure, db.session.begin_nested():
                db.session.execute(text(operation))
            assert failure.value.orig.sqlstate == "42501"

        header = LedgerTransaction(
            **fixture.compensation_scope, kind="allocation", currency="PHP", reason="Direct seal fixture",
            actor_id=fixture.student, payment_event_id=fixture.compensation_source,
        )
        db.session.add(header)
        db.session.flush()
        with pytest.raises(IntegrityError, match="at least two lines"), db.session.begin_nested():
            db.session.execute(text("UPDATE ledger_transactions SET sealed=true WHERE id=:id"), {"id": header.id})
        with pytest.raises(IntegrityError, match="Unsealed"):
            db.session.commit()
        db.session.rollback()


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_postgres_restricted_role_cannot_write_or_seal(postgres_journal, role):
    with postgres_journal.app.app_context():
        db.session.execute(text(f"SET LOCAL ROLE {role}"))
        for operation in (
            "INSERT INTO ledger_accounts (kind, currency) VALUES ('platform_commission', 'PHP')",
            "INSERT INTO admin_audit_events (actor_id, action, target_type, target_id, created_at) "
            "VALUES (1, 'payment_approved', 'payment', 1, CURRENT_TIMESTAMP)",
            "SELECT public.seal_ledger_transaction(1)",
        ):
            with pytest.raises(DBAPIError) as failure, db.session.begin_nested():
                db.session.execute(text(operation))
            assert failure.value.orig.sqlstate == "42501"
