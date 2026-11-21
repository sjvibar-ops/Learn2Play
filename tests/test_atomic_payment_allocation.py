from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from flask_migrate import upgrade
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from __init__ import create_app
from extensions import db
from financial_ledger import _allocation_account, post_transaction
from models import AdminAuditEvent, Appointment, LedgerAccount, LedgerLine, LedgerTransaction, Payment, PaymentEvent, User, utcnow
from payments import record_event
import test_attendance_postgres
from test_payments import PNG


MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")
PARENT = "f78b2c01d4e6"
HEAD = "4f78ec3f41a2"


def isolated_app(database, port, login, *, enabled=False):
    return create_app({
        "TESTING": True, "WTF_CSRF_ENABLED": False,
        "SQLALCHEMY_DATABASE_URI": f"postgresql+psycopg://{login}@127.0.0.1:{port}/{database}",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"options": "-c lock_timeout=10000 -c statement_timeout=15000"}},
        "PAYMENT_LEDGER_APPROVAL_ENABLED": enabled,
    })


def seed_identity(owner):
    with owner.app_context():
        users = [
            User(display_name=role, email=f"{uuid4().hex}@example.test", password_hash="fixture", is_admin=admin)
            for role, admin in (("Student", False), ("Provider", False), ("Admin", True))
        ]
        db.session.add_all(users)
        db.session.flush()
        ids = tuple(user.id for user in users)
        db.session.commit()
        return ids


def seed_payment(owner, identities, *, amount="123.47", currency="PHP", historic=False, reference=None):
    student, provider, admin = identities
    with owner.app_context():
        booking = Appointment(
            student_id=student, provider_id=provider, topic="Isolated allocation test",
            start_time=utcnow(), status="accepted", price=Decimal("123.47"), currency=currency,
            payment_required=True,
        )
        db.session.add(booking)
        db.session.flush()
        payment = Payment(
            appointment_id=booking.id, student_id=student, amount=Decimal(amount), reference=uuid4().hex,
            method="GCash", transaction_reference=reference or uuid4().hex, receipt_path="test/receipt.png",
            submitted_at=utcnow(), submission_revision=1, status="VERIFIED" if historic else "SUBMITTED",
            reviewed_by=admin if historic else None, reviewed_at=utcnow() if historic else None,
        )
        db.session.add(payment)
        db.session.flush()
        record_event(payment, payment.status, "SUBMITTED" if historic else "PENDING", admin if historic else student)
        result = payment.id
        db.session.commit()
        return result


def approve(app, actor, payment, revision=1, action="approve"):
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(actor)
        session["_fresh"] = True
    return client.post(f"/api/admin/payments/{payment}/{action}", json={
        "confirmed": True, "expected_revision": revision, "reason": "Test rejection reason",
    })


@pytest.fixture(scope="module")
def migrated_postgres():
    harness = test_attendance_postgres.PostgresAttendanceConcurrencyTests
    try:
        harness.setUpClass()
    except test_attendance_postgres.unittest.SkipTest as exc:
        pytest.skip(str(exc))
    try:
        # The port and both databases originate in this newly initialized
        # loopback cluster. No configured application URL is consulted.
        with psycopg.connect(
            host="127.0.0.1", port=harness.port, user="l2_attendance_test", dbname="postgres", autocommit=True,
        ) as bootstrap:
            for command in (
                "CREATE ROLE phase2_owner LOGIN", "CREATE ROLE phase2_flask LOGIN",
                "CREATE ROLE learn2play_financial_runtime NOLOGIN",
                "CREATE ROLE anon NOLOGIN", "CREATE ROLE authenticated NOLOGIN",
                "GRANT learn2play_financial_runtime TO phase2_flask",
                "CREATE DATABASE phase2_cutover OWNER phase2_owner",
                "CREATE DATABASE phase2_fresh OWNER phase2_owner",
            ):
                bootstrap.execute(command)
        owner = isolated_app("phase2_cutover", harness.port, "phase2_owner")
        runtime = isolated_app("phase2_cutover", harness.port, "phase2_flask", enabled=True)
        fresh = isolated_app("phase2_fresh", harness.port, "phase2_owner")
        with fresh.app_context():
            upgrade(directory=MIGRATIONS)
            assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
            assert db.session.query(LedgerTransaction).count() == 0
            db.session.remove()
            db.engine.dispose()
        with owner.app_context():
            upgrade(directory=MIGRATIONS, revision=PARENT)
        admin_ids = seed_identity(owner)
        pre_cutover = seed_payment(owner, admin_ids)
        historic = seed_payment(owner, admin_ids, historic=True)
        # Even an enabled, authorized request fails closed before the guard
        # has been installed. No verification is staged in the parent schema.
        before_guard = isolated_app("phase2_cutover", harness.port, "phase2_owner", enabled=True)
        assert approve(before_guard, admin_ids[2], pre_cutover).status_code == 503
        with before_guard.app_context():
            db.engine.dispose()
        with owner.app_context():
            upgrade(directory=MIGRATIONS)
            assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
            assert db.session.execute(text("SELECT session_user")).scalar_one() == "phase2_owner"
            assert db.session.query(LedgerTransaction).count() == 0
            # The normal source-table access is an explicit prerequisite.
            for command in (
                "GRANT SELECT ON users, appointments, payments, payment_events TO phase2_flask",
                "GRANT UPDATE ON appointments, payments TO phase2_flask",
                "GRANT INSERT ON payment_events TO phase2_flask",
                "GRANT USAGE ON SEQUENCE payment_events_id_seq TO phase2_flask",
                "CREATE POLICY phase2_test_source_read ON payment_events FOR SELECT TO phase2_flask USING (true)",
                "CREATE POLICY phase2_test_source_insert ON payment_events FOR INSERT TO phase2_flask WITH CHECK (true)",
            ):
                db.session.execute(text(command))
            db.session.commit()
        with runtime.app_context():
            assert db.session.execute(text("SELECT session_user")).scalar_one() == "phase2_flask"
            assert db.session.execute(text("SELECT public.phase2_approval_ready()")).scalar_one()
            assert not db.session.execute(text(
                "SELECT has_table_privilege(current_user, 'ledger_lines', 'UPDATE')"
            )).scalar_one()
        yield owner, runtime, admin_ids, pre_cutover, historic
    finally:
        harness.tearDownClass()


def journal_details(owner, payment_id):
    with owner.app_context():
        journal = LedgerTransaction.query.filter_by(payment_id=payment_id).one()
        audit = AdminAuditEvent.query.filter_by(verification_event_id=journal.payment_event_id).one()
        items = db.session.query(LedgerLine, LedgerAccount).join(
            LedgerAccount, LedgerAccount.id == LedgerLine.account_id,
        ).filter(LedgerLine.transaction_id == journal.id).order_by(LedgerLine.ordinal).all()
        return journal.sealed, audit.actor_id, [(account.kind, account.provider_id, line.centavos) for line, account in items]


def assert_unapproved(owner, payment_id):
    with owner.app_context():
        assert db.session.get(Payment, payment_id).status == "SUBMITTED"
        assert not PaymentEvent.query.filter_by(payment_id=payment_id, event_type="VERIFIED").count()
        assert not AdminAuditEvent.query.filter_by(target_type="payment", target_id=payment_id).count()
        assert not LedgerTransaction.query.filter_by(payment_id=payment_id).count()


def test_cutover_and_replay(migrated_postgres):
    owner, runtime, identities, submitted, historic = migrated_postgres
    _, provider, admin = identities
    assert approve(runtime, admin, historic).status_code == 409
    assert approve(runtime, admin, submitted, revision=0).status_code == 409
    result = approve(runtime, admin, submitted)
    assert result.status_code == 200
    assert not result.json.get("already_approved")
    assert approve(runtime, admin, submitted).json["already_approved"] is True
    assert journal_details(owner, submitted) == (True, admin, [
        ("payment_clearing", None, 12347), ("platform_commission", None, -2469), ("provider_pending", provider, -9878),
    ])
    with owner.app_context():
        assert not LedgerTransaction.query.filter_by(payment_id=historic).count()


@pytest.mark.parametrize(("amount", "expected"), [
    ("0.01", [1, -1]), ("0.02", [2, -2]), ("0.03", [3, -1, -2]),
])
def test_tiny_payments_use_exact_nonzero_lines(migrated_postgres, amount, expected):
    owner, runtime, identities, _, _ = migrated_postgres
    payment = seed_payment(owner, identities, amount=amount)
    assert approve(runtime, identities[2], payment).status_code == 200
    assert [line[2] for line in journal_details(owner, payment)[2]] == expected


@pytest.mark.parametrize("stage", [
    "INSERT INTO ledger_accounts", "INSERT INTO payment_events", "INSERT INTO admin_audit_events",
    "INSERT INTO ledger_transactions", "INSERT INTO ledger_lines", "SELECT public.seal_ledger_transaction", "commit",
])
def test_all_approval_stages_roll_back_together(migrated_postgres, stage):
    owner, runtime, _, _, _ = migrated_postgres
    identities = seed_identity(owner)
    payment = seed_payment(owner, identities)

    def statement_failure(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith(stage):
            raise SQLAlchemyError("fixture write failure")

    def commit_failure(_conn):
        raise SQLAlchemyError("fixture pre-commit failure")

    hook, callback = ("commit", commit_failure) if stage == "commit" else ("before_cursor_execute", statement_failure)
    with runtime.app_context():
        engine = db.engine
    event.listen(engine, hook, callback)
    try:
        assert approve(runtime, identities[2], payment).status_code == 503
    finally:
        event.remove(engine, hook, callback)
    assert_unapproved(owner, payment)
    with owner.app_context():
        assert not LedgerAccount.query.filter_by(provider_id=identities[1]).count()


def test_runtime_direct_verification_requires_allocation(migrated_postgres):
    owner, runtime, identities, _, _ = migrated_postgres
    payment = seed_payment(owner, identities)
    with runtime.app_context():
        db.session.execute(text(
            "UPDATE payments SET status='VERIFIED', reviewed_by=:actor, reviewed_at=CURRENT_TIMESTAMP WHERE id=:id"
        ), {"id": payment, "actor": identities[2]})
        with pytest.raises(IntegrityError, match="requires its sealed allocation"):
            db.session.commit()
        db.session.rollback()
    assert_unapproved(owner, payment)
    with owner.app_context():
        duplicate = Payment(
            appointment_id=db.session.get(Payment, payment).appointment_id,
            student_id=identities[0], amount=Decimal("1"), reference=uuid4().hex, status="VERIFIED",
        )
        db.session.add(duplicate)
        with pytest.raises(IntegrityError, match="must transition from submitted"):
            db.session.flush()
        db.session.rollback()


def test_concurrent_approvals_and_account_creation(migrated_postgres):
    owner, runtime, _, _, _ = migrated_postgres
    identities = seed_identity(owner)
    first, second = (seed_payment(owner, identities) for _ in range(2))
    barrier = Barrier(3, timeout=15)

    def worker(payment):
        barrier.wait()
        result = approve(runtime, identities[2], payment)
        return result.status_code, result.json.get("already_approved", False)

    with ThreadPoolExecutor(max_workers=3) as workers:
        outcomes = list(workers.map(worker, (first, second, first)))
    assert sorted(outcomes) == [(200, False), (200, False), (200, True)]
    with owner.app_context():
        assert LedgerAccount.query.filter_by(provider_id=identities[1], kind="provider_pending").count() == 1
        assert LedgerTransaction.query.filter(LedgerTransaction.payment_id.in_((first, second))).count() == 2


def test_authorization_switch_and_resubmitted_proof(migrated_postgres, monkeypatch):
    owner, runtime, identities, _, _ = migrated_postgres
    student, provider, admin = identities
    payment = seed_payment(owner, identities)
    assert approve(runtime, provider, payment).status_code == 403
    with monkeypatch.context() as settings:
        settings.setitem(runtime.config, "PAYMENT_LEDGER_APPROVAL_ENABLED", False)
        assert approve(runtime, admin, payment).status_code == 503
    assert_unapproved(owner, payment)
    with owner.app_context():
        booking = db.session.get(Payment, payment).appointment_id
    assert approve(runtime, admin, payment, action="reject").status_code == 200
    client = runtime.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(student)
        session["_fresh"] = True
    with patch("payments.receipts_bucket", return_value=MagicMock()):
        response = client.post(f"/api/payments/bookings/{booking}/submit", data={
            "method": "GCash", "transaction_reference": uuid4().hex,
            "receipt": (BytesIO(PNG), "current.png"),
        })
    assert response.status_code == 200
    assert approve(runtime, admin, payment, revision=1).status_code == 409
    assert approve(runtime, admin, payment, revision=2).status_code == 200


@pytest.mark.parametrize(("amount", "currency"), [("0", "PHP"), ("10", "USD")])
def test_invalid_persisted_amount_or_currency(migrated_postgres, amount, currency):
    owner, runtime, identities, _, _ = migrated_postgres
    payment = seed_payment(owner, identities, amount=amount, currency=currency)
    assert approve(runtime, identities[2], payment).status_code == 409
    assert_unapproved(owner, payment)


def test_uncertain_commit_returns_existing_decision_on_retry(migrated_postgres):
    owner, runtime, identities, _, _ = migrated_postgres
    payment = seed_payment(owner, identities)
    commit = db.session.commit

    def committed_without_acknowledgement():
        commit()
        raise SQLAlchemyError("fixture lost acknowledgement after commit")

    with patch("payments.db.session.commit", side_effect=committed_without_acknowledgement):
        assert approve(runtime, identities[2], payment).status_code == 503
    assert approve(runtime, identities[2], payment).json["already_approved"] is True
    with owner.app_context():
        assert PaymentEvent.query.filter_by(payment_id=payment, event_type="VERIFIED").count() == 1
        assert AdminAuditEvent.query.filter_by(target_type="payment", target_id=payment).count() == 1
        assert LedgerTransaction.query.filter_by(payment_id=payment).count() == 1


@pytest.mark.parametrize("corruption", ["amount", "account", "provider", "event", "audit"])
def test_database_rejects_wrong_allocation_shape_or_provenance(migrated_postgres, corruption):
    owner, runtime, identities, _, historic = migrated_postgres
    payment_id = seed_payment(owner, identities)
    with owner.app_context():
        wrong_event = PaymentEvent.query.filter_by(payment_id=historic, event_type="VERIFIED").one().id

    def incorrect_post(payment, appointment, verification, terms):
        commission, provider = terms
        provider_account = _allocation_account(
            "provider_available" if corruption == "account" else "provider_pending", "PHP",
            identities[0] if corruption == "provider" else appointment.provider_id,
        )
        lines = [
            (_allocation_account("payment_clearing", "PHP", None), commission + provider),
            (_allocation_account("platform_commission", "PHP", None), -commission),
            (provider_account, -provider),
        ]
        if corruption == "amount":
            lines[1] = (lines[1][0], -commission - 1)
            lines[2] = (lines[2][0], -provider + 1)
        return post_transaction(
            kind="allocation", currency="PHP", reason="Fixture boundary check",
            actor_id=payment.reviewed_by, payment_id=payment.id, appointment_id=appointment.id,
            provider_id=appointment.provider_id,
            payment_event_id=wrong_event if corruption == "event" else verification.id, lines=lines,
        )

    from admin_audit import record_admin_event

    def wrong_target(**fields):
        return record_admin_event(**(fields | {"target_id": historic}))

    target, replacement = ("record_admin_event", wrong_target) if corruption == "audit" else (
        "post_initial_allocation", incorrect_post,
    )
    with patch(f"payments.{target}", side_effect=replacement):
        assert approve(runtime, identities[2], payment_id).status_code == 503
    assert_unapproved(owner, payment_id)


def test_duplicate_transfer_claim_still_blocks_approval(migrated_postgres):
    owner, runtime, identities, _, _ = migrated_postgres
    reference = uuid4().hex
    first, second = (seed_payment(owner, identities, reference=reference) for _ in range(2))
    assert approve(runtime, identities[2], first).status_code == 409
    assert approve(runtime, identities[2], second).status_code == 409
    assert_unapproved(owner, first)
    assert_unapproved(owner, second)
