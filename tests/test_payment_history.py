"""Payment audit tests: isolated SQLite, mocked Storage, no migrations."""
import ast
from pathlib import Path
from unittest.mock import patch
import pytest
from sqlalchemy import text, update, delete
from sqlalchemy.exc import SQLAlchemyError, DBAPIError
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql
from alembic.script import ScriptDirectory
from extensions import db
from models import Payment, PaymentEvent, utcnow


@pytest.fixture
def f():
    from test_payments import PaymentTests
    fixture = PaymentTests(methodName='runTest')
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def rows(f):
    return PaymentEvent.query.filter_by(payment_id=f.payment.id).order_by(PaymentEvent.id).all()


def cycle(f):
    assert f.upload().status_code == 200
    first = rows(f)[0]
    old_path, first_id = first.receipt_path, first.id
    assert f.review('reject', reason='Reference not found in receiving account').status_code == 200
    f.login(f.student)
    assert f.upload().status_code == 200
    return old_path, first_id


def test_initial_submission_is_atomic_server_authored_revision(f):
    assert f.upload(revision='900', actor_id=str(f.admin.id), event_type='VERIFIED').status_code == 200
    event, = rows(f)
    assert (event.event_type, event.previous_status, event.new_status, event.revision) == ('SUBMITTED', 'PENDING', 'SUBMITTED', 1)
    assert (event.payment_id, event.appointment_id, event.student_id, event.actor_id) == (f.payment.id, f.booking.id, f.student.id, f.student.id)
    assert event.receipt_path == f.payment.receipt_path
    assert event.reviewer_id is None and event.reviewed_at is None
    assert event.created_at and event.submitted_at == f.payment.submitted_at
    assert f.payment.submission_revision == 1
    assert f.upload().status_code == 409
    assert len(rows(f)) == 1


def test_rejection_resubmission_and_approval_preserve_all_snapshots(f):
    old_path, first_id = cycle(f)
    events = rows(f)
    assert [(e.event_type, e.revision) for e in events] == [('SUBMITTED', 1), ('REJECTED', 1), ('RESUBMITTED', 2)]
    rejected = events[1]
    assert rejected.reviewer_id == f.admin.id
    assert rejected.rejection_reason == 'Reference not found in receiving account'
    assert rejected.reviewed_at is not None
    assert rejected.receipt_path == old_path
    assert events[0].id == first_id
    assert events[2].receipt_path != old_path
    assert f.payment.reviewed_by is None and f.payment.rejection_reason is None
    assert not f.booking.payment_satisfied
    f.storage.remove.assert_not_called()
    assert f.review().status_code == 200
    approved = rows(f)[-1]
    assert (approved.event_type, approved.previous_status, approved.new_status, approved.revision) == ('VERIFIED', 'SUBMITTED', 'VERIFIED', 2)
    assert approved.reviewer_id == f.admin.id and approved.reviewed_at == f.payment.reviewed_at
    assert approved.receipt_path == f.payment.receipt_path
    assert f.booking.payment_satisfied
    assert f.review().status_code == 409
    assert len(rows(f)) == 4


def test_owner_history_hides_reviewer_identity_paths_and_other_students(f):
    old_path, event_id = cycle(f)
    result = f.client.get(f'/api/payments/bookings/{f.booking.id}').json
    assert [e['revision'] for e in result['history']] == [1, 1, 2]
    assert result['history'][1]['rejection_reason']
    assert 'reviewed_by' not in result['payment']
    for event in result['history']:
        assert not {'reviewer_id', 'reviewer', 'actor_id', 'receipt_path'} & set(event)
        assert old_path not in str(event)
    for path in (f'/payments/bookings/{f.booking.id}', '/payments'):
        page = f.client.get(path).get_data(as_text=True)
        assert 'Reference not found in receiving account' in page
        assert 'Attempt 1' in page and 'Attempt 2' in page and 'Current submission' in page
        assert 'Reviewer:' not in page and 'admin #' not in page and old_path not in page
    f.login(f.other)
    assert f.client.get(f'/api/payments/bookings/{f.booking.id}').status_code == 404
    assert f.client.get(f'/payments/bookings/{f.booking.id}').status_code == 404
    assert b'Attempt 1' not in f.client.get('/payments').data


def test_admin_history_only_and_historical_receipt_idor(f):
    old_path, event_id = cycle(f)
    pid, booking_id = f.payment.id, f.booking.id
    url = f'/payments/{pid}/events/{event_id}/receipt'
    f.storage.download.return_value = b'isolated receipt'
    def download(path):
        assert not db.session().in_transaction()
        assert path == old_path
        return b'isolated receipt'
    f.storage.download.side_effect = download
    assert f.client.get(url).status_code == 200
    for actor in (f.other, f.provider):
        f.login(actor)
        assert f.client.get(url).status_code == 404
        assert f.client.get(f'/api/admin/payments/{pid}/history').status_code == 403
        assert f.client.get('/admin/payments').status_code == 403
    f.login(f.student)
    assert f.client.get(f'/api/admin/payments/{pid}/history').status_code == 403
    f.login(f.admin)
    response = f.client.get(f'/api/admin/payments/{pid}/history')
    assert response.status_code == 200
    rejected = response.json['history'][1]
    assert rejected['reviewer_id'] == f.admin.id and rejected['reviewer'] == f.admin.display_name
    assert 'receipt_path' not in rejected
    assert f.client.get(url).status_code == 200
    assert f.client.get(f'/payments/{pid+999}/events/{event_id}/receipt').status_code == 404
    assert f.client.get(f'/api/admin/payments/{pid+999}/history').status_code == 404
    page = f.client.get('/admin/payments?status=ALL').get_data(as_text=True)
    assert 'Reviewer: Admin' in page and 'Attempt 1' in page and 'Attempt 2' in page


@pytest.mark.parametrize('verb', ['post', 'put', 'patch', 'delete'])
def test_no_student_event_mutation_endpoints(f, verb):
    assert f.upload().status_code == 200
    event = rows(f)[0]
    for path in (f'/payments/{f.payment.id}/events/{event.id}/receipt', f'/api/payments/bookings/{f.booking.id}'):
        assert getattr(f.client, verb)(path, json={'new_status':'VERIFIED'}).status_code in (404, 405)
    assert rows(f)[0].new_status == 'SUBMITTED'
    assert f.payment.status == 'SUBMITTED'


def test_orm_and_raw_sql_update_delete_guards(f):
    assert f.upload().status_code == 200
    event = rows(f)[0]
    event_id = event.id
    event.rejection_reason = 'tampered'
    with pytest.raises(ValueError, match='append-only'):
        db.session.commit()
    db.session.rollback()
    db.session.delete(db.session.get(PaymentEvent, event_id))
    with pytest.raises(ValueError, match='append-only'):
        db.session.commit()
    db.session.rollback()
    for statement in (update(PaymentEvent).where(PaymentEvent.id == event_id).values(new_status='VERIFIED'), delete(PaymentEvent).where(PaymentEvent.id == event_id)):
        with pytest.raises(DBAPIError, match='append-only'):
            db.session.execute(statement)
            db.session.commit()
        db.session.rollback()
    assert db.session.get(PaymentEvent, event_id).new_status == 'SUBMITTED'


def test_audit_insert_failure_rolls_back_submission_and_only_discards_new_upload(f):
    from payments import record_event
    def fail(*args, **kwargs):
        record_event(*args, **kwargs)
        raise SQLAlchemyError('isolated audit failure')
    with patch('payments.record_event', side_effect=fail):
        response = f.upload()
    assert response.status_code == 503
    assert f.payment.status == 'PENDING' and f.payment.submission_revision == 0
    assert f.payment.receipt_path is None and rows(f) == []
    f.storage.remove.assert_called_once_with([f.storage.upload.call_args.args[0]])


@pytest.mark.parametrize("failure_point", ["flush", "commit"])
def test_failed_resubmission_preserves_previous_event_and_review(f, failure_point):
    assert f.upload().status_code == 200
    assert f.review('reject', reason='Receipt does not match transfer').status_code == 200
    f.login(f.student)
    old = f.payment.receipt_path
    with patch.object(db.session, failure_point, side_effect=SQLAlchemyError("isolated write failure")):
        assert f.upload().status_code == 503
    assert f.payment.status == 'REJECTED' and f.payment.submission_revision == 1
    assert f.payment.receipt_path == old
    assert len(rows(f)) == 2
    if failure_point == "flush":
        f.storage.remove.assert_called_once_with([f.storage.upload.call_args.args[0]])
    else:
        f.storage.remove.assert_not_called()


def test_failed_review_rolls_back_both_projection_and_event(f):
    assert f.upload().status_code == 200
    f.login(f.admin)
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('isolated failure')):
        assert f.review('reject', reason='Receipt does not match transfer').status_code == 503
    assert f.payment.status == 'SUBMITTED' and f.payment.reviewed_by is None
    assert len(rows(f)) == 1


def test_legacy_snapshot_is_factual_and_only_created_on_successful_transition(f):
    f.payment.status = 'REJECTED'
    f.payment.receipt_path = 'legacy/private.png'
    f.payment.reviewed_by = f.admin.id
    f.payment.reviewed_at = utcnow()
    f.payment.rejection_reason = 'Known legacy rejection'
    db.session.commit()
    assert f.client.get(f'/api/payments/bookings/{f.booking.id}').json['history'] == []
    assert f.upload().status_code == 200
    legacy, submission = rows(f)
    assert (legacy.event_type, legacy.revision, legacy.previous_status, legacy.new_status) == ('LEGACY_SNAPSHOT', 0, 'REJECTED', 'REJECTED')
    assert legacy.receipt_path == 'legacy/private.png'
    assert legacy.reviewer_id == f.admin.id and legacy.rejection_reason == 'Known legacy rejection'
    assert legacy.submitted_at is None  # No invented submission time.
    assert submission.revision == 1 and submission.event_type == 'RESUBMITTED'
    f.storage.remove.assert_not_called()


def test_legacy_submission_review_keeps_revision_zero_without_guessing_attempts(f):
    f.payment.status = 'SUBMITTED'
    f.payment.method = 'GCash'
    f.payment.transaction_reference = 'KNOWN-LEGACY-TRANSFER'
    f.payment.receipt_path = 'legacy/receipt.png'
    f.payment.submitted_at = utcnow()
    db.session.commit()
    assert f.review().status_code == 200
    snapshot, decision = rows(f)
    assert snapshot.event_type == 'LEGACY_SNAPSHOT' and snapshot.revision == 0
    assert snapshot.previous_status == snapshot.new_status == 'SUBMITTED'
    assert decision.event_type == 'VERIFIED' and decision.revision == 0
    assert decision.reviewer_id == f.admin.id
    assert snapshot.reviewed_at is None and decision.reviewed_at is not None


def test_history_events_do_not_authorize_payment_or_award_xp(f):
    from models import XpEvent
    from payments import record_event
    # Even an inconsistent privileged audit insertion cannot authorize payment.
    event = record_event(f.payment, 'VERIFIED', 'SUBMITTED', f.admin.id)
    event.new_status = 'VERIFIED'
    event.reviewer_id = f.admin.id
    event.reviewed_at = utcnow()
    f.booking.status = 'accepted'
    f.booking.meeting_room = 'audit-does-not-authorize'
    db.session.commit()
    assert f.payment.status == 'PENDING'
    assert not f.booking.payment_satisfied
    assert f.client.get(f'/api/sessions/{f.booking.id}/join').status_code == 403
    assert XpEvent.query.count() == 0


def test_new_migration_is_additive_frozen_and_single_head_without_execution():
    root = Path(__file__).resolve().parents[1]
    scripts = ScriptDirectory(str(root/'migrations'))
    assert scripts.get_heads() == ['4f78ec3f41a2']
    assert scripts.get_revision('e9b7c2d41a60').down_revision == 'c7e4a1b92d60'
    source = (root/'migrations/versions/e9b7c2d41a60_payment_audit_history.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    upgrade = ast.unparse(next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'upgrade'))
    assert "server_default='0'" in upgrade
    assert not any(word in upgrade for word in ('drop_table', 'drop_column', 'INSERT INTO', 'UPDATE payments', 'DELETE FROM'))
    assert 'ENABLE ROW LEVEL SECURITY' in source and 'REVOKE ALL' in source
    assert 'BEFORE TRUNCATE' in source and 'BEFORE UPDATE OR DELETE' in source
    assert 'raise RuntimeError' in ast.unparse(next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'downgrade'))
    ddl = str(CreateTable(PaymentEvent.__table__).compile(dialect=postgresql.dialect()))
    assert 'REFERENCES payments (id) ON DELETE RESTRICT' in ddl
    assert 'uq_payment_event_revision_type' in ddl

    from models import PAYMENT_EVENT_POSTGRES_GUARDS
    frozen_sql = {
        " ".join(node.args[0].value.split())
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'execute' and node.args
        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
    }
    assert {" ".join(sql.split()) for sql in PAYMENT_EVENT_POSTGRES_GUARDS} <= frozen_sql
