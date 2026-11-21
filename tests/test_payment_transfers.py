"""Duplicate transfer regression tests; storage is mocked, SQLite is in-memory."""
from datetime import timedelta
from decimal import Decimal
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex
from sqlalchemy.dialects import postgresql, sqlite

from extensions import db
from models import Appointment, Payment, utcnow
from payments import new_payment


@pytest.fixture
def payment_fixture():
    from test_payments import PaymentTests
    fixture = PaymentTests(methodName='runTest')
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def submit_claim(fixture, reference, method='GCash', student=None):
    from test_payments import PNG
    owner = student or fixture.student
    booking = Appointment(student_id=owner.id, provider_id=fixture.provider.id,
                          topic='Transfer allocation test', start_time=utcnow()+timedelta(days=3),
                          status='accepted', price=Decimal('123.45'), payment_required=True)
    db.session.add(booking)
    db.session.flush()
    payment = new_payment(booking)
    db.session.add(payment)
    db.session.commit()
    fixture.login(owner)
    response = fixture.client.post(f'/api/payments/bookings/{booking.id}/submit',data={
        'receipt':(io.BytesIO(PNG),'receipt.png'), 'method':method,
        'transaction_reference':reference,
    })
    assert response.status_code == 200
    assert payment.status == 'SUBMITTED'
    return payment


def decide(fixture, payment, action='approve', reason='Incorrect transfer claim confirmed by admin'):
    fixture.login(fixture.admin)
    return fixture.client.post(f'/api/admin/payments/{payment.id}/{action}',json={
        'confirmed':True,'reason':reason,'expected_revision':payment.submission_revision,
    })


def test_duplicate_submissions_still_work_and_all_unresolved_claims_block_review(payment_fixture):
    f = payment_fixture
    first = submit_claim(f,'  Tx-1234  ')
    second = submit_claim(f,'tx-1234',student=f.other)
    assert first.transaction_reference == 'Tx-1234'
    assert f.storage.upload.call_count == 2
    assert decide(f,first).status_code == 409
    assert decide(f,second).status_code == 409
    assert (first.status,second.status) == ('SUBMITTED','SUBMITTED')
    page = f.client.get('/admin/payments')
    assert page.status_code == 200
    assert b'Possible duplicate transfer claim' in page.data
    assert first.reference.encode() in page.data and second.reference.encode() in page.data
    assert b'Approval blocked' in page.data
    assert b'value="approve" disabled' in page.data
    f.login(f.other)
    assert f.client.get(f'/payments/bookings/{second.appointment_id}').status_code == 200
    assert f.client.get(f'/payments/bookings/{first.appointment_id}').status_code == 404
    assert f.client.get('/admin/payments').status_code == 403
    own = f.client.get(f'/api/payments/bookings/{second.appointment_id}')
    assert first.reference not in own.get_data(as_text=True)


def test_reject_incorrect_claim_then_approve_legitimate_and_allow_resubmission(payment_fixture):
    from test_payments import PNG
    f = payment_fixture
    first = submit_claim(f,'TX-2345')
    second = submit_claim(f,'tx-2345')
    assert decide(f,second,'reject').status_code == 200
    page = f.client.get(f'/admin/payments?status=ALL&payment_id={first.id}')
    assert b'Matching claims are rejected or not yet submitted' in page.data
    assert b'value="approve" disabled' not in page.data
    assert decide(f,first).status_code == 200
    assert first.status == 'VERIFIED'
    f.login(f.student)
    # A reserved reference does not prevent submission, including resubmission.
    response=f.client.post(f'/api/payments/bookings/{second.appointment_id}/submit',data={
        'receipt':(io.BytesIO(PNG),'replacement.png'),'method':'GCash','transaction_reference':' TX-2345 ',
    })
    assert response.status_code == 200
    assert decide(f,second).status_code == 409
    assert second.status == 'SUBMITTED'
    assert second.reviewed_by is None and second.reviewed_at is None
    assert decide(f,second,'reject').status_code == 200
    f.login(f.student)
    response=f.client.post(f'/api/payments/bookings/{second.appointment_id}/submit',data={
        'receipt':(io.BytesIO(PNG),'corrected.png'),'method':'GCash','transaction_reference':'GENUINE-NEW-TRANSFER',
    })
    assert response.status_code == 200
    assert decide(f,second).status_code == 200
    assert first.amount == second.amount == Decimal('123.45')
    assert f.storage.remove.call_count == 0  # Historical receipt revisions are retained.


def test_same_reference_under_different_methods_is_not_a_conflict(payment_fixture):
    f = payment_fixture
    first=submit_claim(f,'SAME-4567',method='GCash')
    second=submit_claim(f,'same-4567',method='Maya')
    assert decide(f,first).status_code == 200
    assert decide(f,second).status_code == 200
    assert (first.status,second.status)==('VERIFIED','VERIFIED')


def test_database_unique_index_normalizes_case_and_spaces_only_for_verified_rows(payment_fixture):
    f=payment_fixture
    first=submit_claim(f,'Case-5678')
    second=submit_claim(f,'case-5678')
    first.status='VERIFIED'
    db.session.commit()
    second.transaction_reference='  CASE-5678  '
    second.status='VERIFIED'
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert first.status=='VERIFIED' and second.status=='SUBMITTED'
    # Rejected and submitted rows are intentionally not unique reservations.
    second.status='REJECTED'
    db.session.commit()
    assert second.status=='REJECTED'


def test_database_conflict_returns_409_and_rolls_back_losing_review(payment_fixture):
    f=payment_fixture
    first=submit_claim(f,'TX-6789')
    assert decide(f,first).status_code==200
    second=submit_claim(f,'tx-6789')
    # Simulate a stale application precheck; the database remains authoritative.
    with patch('payments.transfer_claims',return_value=[]):
        result=decide(f,second)
    assert result.status_code==409
    assert 'already verified' in result.json['message']
    assert second.status=='SUBMITTED'
    assert second.reviewed_by is None and second.reviewed_at is None
    assert not second.appointment.payment_satisfied


def test_cancellation_does_not_release_a_verified_transfer(payment_fixture):
    f=payment_fixture
    first=submit_claim(f,'TX-7890')
    assert decide(f,first).status_code==200
    first.appointment.status='cancelled'
    db.session.commit()
    second=submit_claim(f,'tx-7890')
    assert decide(f,second).status_code==409


def test_missing_transfer_identity_cannot_be_approved(payment_fixture):
    f=payment_fixture
    f.payment.status='SUBMITTED'
    db.session.commit()
    assert decide(f,f.payment).status_code==409
    assert f.payment.status=='SUBMITTED'


def test_admin_can_open_flagged_claim_and_invalid_filter_is_rejected(payment_fixture):
    f=payment_fixture
    first=submit_claim(f,'TX-8901')
    second=submit_claim(f,'tx-8901')
    f.login(f.admin)
    page=f.client.get(f'/admin/payments?status=ALL&payment_id={second.id}')
    assert page.status_code==200
    assert f'payment_id={first.id}'.encode() in page.data
    assert f.client.get('/admin/payments?payment_id=bad').status_code==400


def test_migration_and_model_index_definitions_without_running_migrations():
    index=next(index for index in Payment.__table__.indexes if index.name=='uq_payments_verified_transfer')
    for dialect in (postgresql.dialect(),sqlite.dialect()):
        ddl=str(CreateIndex(index).compile(dialect=dialect))
        assert 'CREATE UNIQUE INDEX' in ddl
        assert 'lower(trim(method))' in ddl
        assert 'lower(trim(transaction_reference))' in ddl
        assert "WHERE status = 'VERIFIED'" in ddl
    source=(Path(__file__).resolve().parents[1]/'migrations/versions/c7e4a1b92d60_unique_verified_transfer.py').read_text()
    assert "down_revision = 'b9d3e7f02c84'" in source
    assert 'HAVING count(*) > 1' in source
    assert 'Existing duplicate VERIFIED' in source
    assert 'op.drop_table' not in source


def test_pending_record_does_not_reserve_a_transfer_or_block_submission(payment_fixture):
    from test_payments import PNG
    f=payment_fixture
    f.payment.method='GCash'
    f.payment.transaction_reference='PENDING-9012'
    db.session.commit()
    submitted=submit_claim(f,'pending-9012')
    assert decide(f,submitted).status_code==200
    f.login(f.student)
    response=f.client.post(f'/api/payments/bookings/{f.booking.id}/submit',data={
        'receipt':(io.BytesIO(PNG),'receipt.png'),'method':'GCash','transaction_reference':' PENDING-9012 ',
    })
    assert response.status_code==200
    assert decide(f,f.payment).status_code==409
