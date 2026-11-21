"""Financial recordkeeping only: demo attestations, SQLite and mocked Storage."""
import ast
from pathlib import Path
from unittest.mock import patch
import pytest
from sqlalchemy.exc import SQLAlchemyError
from alembic.script import ScriptDirectory
from extensions import db
from models import Appointment, Payment, PaymentEvent, XpEvent, utcnow


@pytest.fixture
def f():
    from test_payments import PaymentTests
    fixture = PaymentTests(methodName='runTest')
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def prepare(f, status='SUBMITTED'):
    if status != 'PENDING':
        assert f.upload().status_code == 200
    if status == 'VERIFIED':
        assert f.review().status_code == 200
    elif status == 'REJECTED':
        assert f.review('reject', reason='Independent receiving records did not match').status_code == 200
    f.login(f.student)


def ending(f, action='cancel', admin=False, reason='Demo booking ending reason'):
    f.login(f.admin if admin else f.provider if action == 'reject' else f.student)
    prefix = '/api/admin/bookings' if admin else '/api/bookings'
    return f.client.post(f'{prefix}/{f.booking.id}/{action}', json={'reason': reason})


def payload(f, state='NO_FUNDS_RECEIVED', **overrides):
    data = dict(state=state, expected_version=f.payment.reconciliation_version,
                reason='INTERNAL demo financial investigation reason',
                student_explanation='Student-safe explanation of the recorded external financial outcome.',
                confirmation_notes='INTERNAL DEMO independent authorized-account check or external confirmation notes.',
                evidence_reference='INTERNAL-DEMO-CASE-REF', authority_name='DEMO external account holder',
                authority_capacity='account_holder')
    flags = {
        'NO_FUNDS_RECEIVED': {'independent_check_confirmed':True},
        'REFUND_PENDING': {'funds_received_confirmed':True, 'authorized_refund_confirmed':True},
        'REFUNDED': {'account_holder_refund_confirmed':True},
        'RETAINED': {'funds_received_confirmed':True, 'authorized_retention_confirmed':True},
    }
    data.update(flags.get(state, {}))
    data.update(overrides)
    return data


def decide(f, state='NO_FUNDS_RECEIVED', **overrides):
    f.login(f.admin)
    return f.client.post(f'/api/admin/payments/{f.payment.id}/reconcile', json=payload(f,state,**overrides))


@pytest.mark.parametrize('status', ['PENDING','SUBMITTED','VERIFIED','REJECTED'])
@pytest.mark.parametrize('action,admin', [('cancel',False),('reject',False),('cancel',True),('reject',True)])
def test_all_booking_endings_preserve_payment_and_flag_evidence(f, status, action, admin):
    prepare(f,status)
    before = [(e.id,e.event_type,e.receipt_path,e.reviewed_at,e.reviewer_id,e.rejection_reason) for e in f.payment.events]
    original = (f.payment.status,f.payment.amount,f.payment.reference,f.payment.reviewed_at,f.payment.reviewed_by,f.payment.receipt_path)
    result = ending(f,action,admin)
    assert result.status_code == 200
    assert f.booking.status == ('rejected' if action == 'reject' else 'cancelled')
    assert f.booking.ended_at and f.booking.ended_by_id
    assert f.booking.ending_reason == 'Demo booking ending reason'
    assert (f.payment.status,f.payment.amount,f.payment.reference,f.payment.reviewed_at,f.payment.reviewed_by,f.payment.receipt_path) == original
    assert f.payment.reconciliation_state == ('NOT_REQUIRED' if status == 'PENDING' else 'NEEDS_REVIEW')
    assert f.payment.reconciliation_version == 1
    events = PaymentEvent.query.filter_by(payment_id=f.payment.id).order_by(PaymentEvent.id).all()
    assert [(e.id,e.event_type,e.receipt_path,e.reviewed_at,e.reviewer_id,e.rejection_reason) for e in events[:-1]] == before
    event = events[-1]
    assert event.event_type == 'BOOKING_ENDED'
    assert event.previous_status == event.new_status == status
    assert event.booking_ended_at == f.booking.ended_at
    assert event.booking_status == f.booking.status and event.booking_ending_reason == f.booking.ending_reason
    assert event.reconciliation_previous_state == 'NOT_REQUIRED'
    assert event.reconciliation_state == f.payment.reconciliation_state
    assert XpEvent.query.count() == 0
    f.storage.remove.assert_not_called()


def test_normal_participant_requests_without_reason_remain_compatible(f):
    f.login(f.student)
    assert f.client.post(f'/api/bookings/{f.booking.id}/cancel').status_code == 200
    assert f.booking.ending_reason == 'Cancelled by the assigned student.'


def test_provider_can_cancel_but_unrelated_user_cannot_end_booking(f):
    f.login(f.other)
    assert f.client.post(f'/api/bookings/{f.booking.id}/cancel').status_code == 403
    assert f.client.post(f'/api/bookings/{f.booking.id}/reject').status_code == 403
    f.login(f.provider)
    assert f.client.post(f'/api/bookings/{f.booking.id}/cancel').status_code == 200
    assert f.booking.ending_reason == 'Cancelled by the assigned provider.'


@pytest.mark.parametrize('state,missing', [
    ('NO_FUNDS_RECEIVED','independent_check_confirmed'),
    ('REFUND_PENDING','funds_received_confirmed'),
    ('REFUND_PENDING','authorized_refund_confirmed'),
    ('RETAINED','funds_received_confirmed'),
    ('RETAINED','authorized_retention_confirmed'),
])
def test_required_external_checks_are_not_implied_by_admin_access_or_receipt(f,state,missing):
    prepare(f)
    assert ending(f).status_code == 200
    assert decide(f,state,**{missing:False}).status_code == 400
    assert decide(f,state,**{missing:'true'}).status_code == 400
    assert f.payment.reconciliation_state == 'NEEDS_REVIEW'
    assert f.payment.status == 'SUBMITTED'


@pytest.mark.parametrize('field', ['reason','student_explanation','confirmation_notes','authority_name','authority_capacity'])
def test_retention_requires_documented_reason_and_external_authority(f,field):
    prepare(f,'VERIFIED')
    assert ending(f).status_code == 200
    assert decide(f,'RETAINED',**{field:''}).status_code == 400
    assert f.payment.reconciliation_state == 'NEEDS_REVIEW'


def test_refund_completion_is_a_separate_explicit_account_holder_confirmation(f):
    prepare(f,'VERIFIED')
    original_review = (f.payment.reviewed_at,f.payment.reviewed_by)
    assert ending(f).status_code == 200
    assert decide(f,'REFUNDED').status_code == 409  # Cannot skip pending authorization.
    assert decide(f,'REFUND_PENDING', authority_capacity='authorized_financial_decision_maker').status_code == 200
    assert f.payment.reconciliation_state == 'REFUND_PENDING'
    assert decide(f,'REFUNDED',account_holder_refund_confirmed=False).status_code == 400
    assert decide(f,'REFUNDED',authority_capacity='authorized_financial_decision_maker').status_code == 400
    response = decide(f,'REFUNDED')
    assert response.status_code == 200
    assert 'did not process a refund' in response.json['message']
    assert f.payment.reconciliation_state == 'REFUNDED' and f.payment.status == 'VERIFIED'
    assert (f.payment.reviewed_at,f.payment.reviewed_by) == original_review
    assert decide(f,'RETAINED').status_code == 409
    events = PaymentEvent.query.filter_by(payment_id=f.payment.id).order_by(PaymentEvent.id).all()
    assert [e.event_type for e in events] == ['SUBMITTED','VERIFIED','BOOKING_ENDED','REFUND_PENDING','REFUNDED']
    assert [(e.reconciliation_previous_state,e.reconciliation_state) for e in events[-2:]] == [('NEEDS_REVIEW','REFUND_PENDING'),('REFUND_PENDING','REFUNDED')]
    assert events[-1].account_holder_refund_confirmed is True
    assert events[-1].authority_name == 'DEMO external account holder'
    assert events[-1].actor_id == f.admin.id
    assert XpEvent.query.count() == 0


@pytest.mark.parametrize('state', ['NO_FUNDS_RECEIVED','RETAINED'])
def test_final_outcomes_are_append_only_and_do_not_rewrite_original_verification(f,state):
    prepare(f,'VERIFIED')
    assert ending(f).status_code == 200
    old_version = f.payment.reconciliation_version
    assert decide(f,state).status_code == 200
    assert f.payment.status == 'VERIFIED'
    assert decide(f,state,expected_version=old_version).status_code == 409
    assert f.payment.reconciliation_version == old_version+1
    event = PaymentEvent.query.filter_by(payment_id=f.payment.id,event_type=state).one()
    assert event.decision_reason.startswith('INTERNAL')
    assert event.confirmation_notes.startswith('INTERNAL')
    assert event.reconciliation_explanation.startswith('Student-safe')


def test_no_funds_finding_cannot_also_claim_received_funds(f):
    prepare(f)
    ending(f)
    assert decide(f,'NO_FUNDS_RECEIVED',funds_received_confirmed=True).status_code == 400


def test_late_evidence_can_be_flagged_without_implying_funds_received(f):
    assert ending(f).status_code == 200
    assert f.payment.reconciliation_state == 'NOT_REQUIRED'
    assert decide(f,'NEEDS_REVIEW').status_code == 200
    assert f.payment.status == 'PENDING'
    assert decide(f,'NO_FUNDS_RECEIVED').status_code == 200


def test_unauthorized_roles_cannot_access_queue_decide_or_admin_endings(f):
    prepare(f)
    ending(f)
    for actor in (f.student,f.other,f.provider):
        f.login(actor)
        assert f.client.get('/admin/payments/reconciliation').status_code == 403
        assert f.client.post(f'/api/admin/payments/{f.payment.id}/reconcile',json=payload(f)).status_code == 403
        assert f.client.post(f'/api/admin/bookings/{f.booking.id}/cancel',json={'reason':'Not authorized'}).status_code == 403
    assert f.payment.reconciliation_state == 'NEEDS_REVIEW'


def test_student_public_explanation_and_history_never_expose_internal_confirmation_notes(f):
    prepare(f)
    ending(f)
    assert decide(f,'RETAINED').status_code == 200
    f.login(f.student)
    response = f.client.get(f'/api/payments/bookings/{f.booking.id}')
    assert response.status_code == 200
    assert response.json['payment']['reconciliation_state'] == 'RETAINED'
    assert 'INTERNAL' not in response.get_data(as_text=True)
    assert 'DEMO external account holder' not in response.get_data(as_text=True)
    for path in ('/payments',f'/payments/bookings/{f.booking.id}'):
        page = f.client.get(path).get_data(as_text=True)
        assert 'Student-safe explanation' in page and 'RETAINED' in page
        assert 'INTERNAL' not in page and 'DEMO external account holder' not in page
    f.login(f.other)
    assert f.client.get(f'/api/payments/bookings/{f.booking.id}').status_code == 404
    f.login(f.provider)
    own_booking = next(row for row in f.client.get('/api/bookings').json['bookings'] if row['id']==f.booking.id)
    assert own_booking['reconciliation_state'] is None
    assert own_booking['reconciliation_explanation'] is None
    assert own_booking['payment_reference'] is None and own_booking['payment_url'] is None
    f.login(f.admin)
    history = f.client.get(f'/api/admin/payments/{f.payment.id}/history').json['history'][-1]
    assert history['decision_reason'].startswith('INTERNAL') and history['authority_name']
    page = f.client.get('/admin/payments/reconciliation?state=ALL')
    assert page.status_code == 200
    assert b'INTERNAL' in page.data and b'Confirmation recording only' in page.data
    assert b'class="payment-review"' not in page.data


def test_cancelled_and_rejected_sessions_stay_blocked_for_payment_jaas_and_attendance(f):
    prepare(f,'VERIFIED')
    assert ending(f,'reject').status_code == 200
    assert decide(f,'NO_FUNDS_RECEIVED').status_code == 200
    for actor in (f.student,f.provider):
        f.login(actor)
        assert f.client.get(f'/api/sessions/{f.booking.id}/join').status_code == 403
        with patch('routes.create_jaas_participant_token') as token:
            assert f.client.get(f'/sessions/{f.booking.id}/meeting').status_code == 403
            token.assert_not_called()
        for action in ('complete','confirm-attendance','dispute-attendance','accept'):
            assert f.client.post(f'/api/bookings/{f.booking.id}/{action}',json={'note':'Demo disputed attendance'}).status_code in (403,409)
    f.login(f.student)
    assert f.upload().status_code == 409
    assert f.review().status_code == 409
    assert XpEvent.query.count() == 0


def test_financial_actions_on_active_booking_or_with_stale_versions_fail(f):
    prepare(f)
    assert decide(f,'REFUND_PENDING').status_code == 409
    assert ending(f).status_code == 200
    for version in (True,'1',-1,None):
        assert decide(f,expected_version=version).status_code == 409
    assert f.payment.reconciliation_state == 'NEEDS_REVIEW'


def test_cancellation_failure_rolls_back_ending_reconciliation_and_audit(f):
    prepare(f)
    before = PaymentEvent.query.count()
    f.login(f.student)
    with patch.object(db.session,'commit',side_effect=SQLAlchemyError('isolated failure')):
        response = f.client.post(f'/api/bookings/{f.booking.id}/cancel')
    assert response.status_code == 503
    assert f.booking.status == 'pending' and f.booking.ended_at is None
    assert f.payment.reconciliation_state == 'NOT_REQUIRED'
    assert PaymentEvent.query.count() == before


def test_reconciliation_failure_rolls_back_state_and_event(f):
    prepare(f)
    ending(f)
    before = PaymentEvent.query.count()
    f.login(f.admin)
    with patch.object(db.session,'commit',side_effect=SQLAlchemyError('isolated failure')):
        response = f.client.post(f'/api/admin/payments/{f.payment.id}/reconcile',json=payload(f))
    assert response.status_code == 503
    assert f.payment.reconciliation_state == 'NEEDS_REVIEW'
    assert f.payment.reconciliation_version == 1
    assert PaymentEvent.query.count() == before


def test_admin_ending_requires_reason_and_pending_status_for_rejection(f):
    f.login(f.admin)
    assert f.client.post(f'/api/admin/bookings/{f.booking.id}/cancel',json={}).status_code == 400
    assert f.booking.status == 'pending'
    f.booking.status = 'accepted'
    db.session.commit()
    assert ending(f,'reject',True).status_code == 409
    assert ending(f,'cancel',True).status_code == 200


@pytest.mark.parametrize('exempt', [False,True])
def test_free_and_exempt_bookings_end_without_creating_payment_or_xp(f,exempt):
    appointment = Appointment(provider_id=f.provider.id,student_id=f.student.id,topic='Free or legacy exempt',
                              start_time=utcnow(),status='accepted',price=0 if not exempt else 100,payment_required=False)
    db.session.add(appointment)
    db.session.commit()
    aid = appointment.id
    assert f.client.post(f'/api/bookings/{aid}/cancel').status_code == 200
    assert appointment.status == 'cancelled' and appointment.ended_at
    assert Payment.query.filter_by(appointment_id=aid).first() is None
    assert XpEvent.query.count() == 0


def test_csrf_and_admin_endpoint_validation(f):
    prepare(f)
    ending(f)
    f.login(f.admin)
    assert f.client.get('/admin/payments/reconciliation?state=INVALID').status_code == 400
    assert f.client.post(f'/api/admin/payments/{f.payment.id}/reconcile',json=[]).status_code == 400
    assert f.client.post('/api/admin/payments/999999/reconcile',json=payload(f)).status_code == 404
    f.app.config['WTF_CSRF_ENABLED'] = True
    assert f.client.post(f'/api/admin/payments/{f.payment.id}/reconcile',json=payload(f)).status_code == 400
    assert f.client.post(f'/api/admin/bookings/{f.booking.id}/cancel',json={'reason':'Demo reason'}).status_code == 400


def test_reconciliation_migration_is_conservative_single_head_and_not_executed():
    root = Path(__file__).resolve().parents[1]
    graph = ScriptDirectory(str(root/'migrations'))
    assert graph.get_heads() == ['83ca40c9f279']
    assert graph.get_revision('f4c2a8d91e70').down_revision == 'e9b7c2d41a60'
    source = (root/'migrations/versions/f4c2a8d91e70_payment_reconciliation.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    upgrade = ast.unparse(next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='upgrade'))
    assert 'drop_table' not in upgrade and 'drop_column' not in upgrade
    assert "reconciliation_state = 'NEEDS_REVIEW'" in source
    assert "SET status" not in source and 'INSERT INTO payment_events' not in source
    assert 'CREATE TRIGGER IF NOT EXISTS' in source
    assert 'raise RuntimeError' in ast.unparse(next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='downgrade'))


@pytest.mark.parametrize('field', ['reason','confirmation_notes'])
def test_no_funds_requires_documented_check_and_reason(f,field):
    prepare(f)
    ending(f)
    assert decide(f,'NO_FUNDS_RECEIVED',**{field:''}).status_code == 400
    assert f.payment.reconciliation_state == 'NEEDS_REVIEW'


def test_reconciliation_preserves_attendance_dispute_and_confirmations(f):
    from datetime import timedelta
    from models import AttendanceEvent
    prepare(f,'VERIFIED')
    f.booking.status = 'accepted'
    f.booking.start_time = utcnow()-timedelta(hours=2)
    db.session.commit()
    f.login(f.provider)
    assert f.client.post(f'/api/bookings/{f.booking.id}/complete').status_code == 200
    f.login(f.student)
    assert f.client.post(f'/api/bookings/{f.booking.id}/dispute-attendance',json={'note':'Demo attendance disagreement'}).status_code == 200
    confirmations = (f.booking.student_confirmed_at,f.booking.provider_confirmed_at,f.booking.attendance_disputed_at)
    before = [(e.id,e.action,e.actor_id,e.created_at) for e in AttendanceEvent.query.all()]
    assert ending(f).status_code == 200
    assert decide(f,'REFUND_PENDING').status_code == 200
    assert (f.booking.student_confirmed_at,f.booking.provider_confirmed_at,f.booking.attendance_disputed_at) == confirmations
    assert [(e.id,e.action,e.actor_id,e.created_at) for e in AttendanceEvent.query.all()] == before
    assert f.client.post(f'/api/admin/attendance/{f.booking.id}/resolve',json={'outcome':'reopen','reason':'Demo attempt to reopen ended booking'}).status_code == 409
    assert XpEvent.query.count() == 0


@pytest.mark.parametrize('state', ['REFUNDED','RETAINED','NO_FUNDS_RECEIVED'])
def test_reconciliation_never_releases_verified_transfer_allocation(f,state):
    from test_payment_transfers import submit_claim, decide as verify_claim
    prepare(f,'VERIFIED')
    ending(f)
    if state == 'REFUNDED':
        assert decide(f,'REFUND_PENDING').status_code == 200
    assert decide(f,state).status_code == 200
    other = submit_claim(f,' tx-123456 ',student=f.other)
    assert verify_claim(f,other).status_code == 409
    assert f.payment.status == 'VERIFIED' and other.status == 'SUBMITTED'


def test_reconciliation_javascript_serializes_explicit_confirmations_and_csrf():
    import shutil
    import subprocess
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js unavailable')
    subprocess.run([node,str(Path(__file__).with_name('payment_reconciliation.cjs'))],check=True,timeout=30)


def test_payment_appearing_between_lock_reads_forces_retry_instead_of_missing_reconciliation(f):
    prepare(f)
    original_count = PaymentEvent.query.count()
    # Simulate the initial Payment read predating a just-committed booking/payment.
    with patch('reconciliation.locked_payment',return_value=None):
        assert ending(f).status_code == 409
    assert f.booking.status == 'pending' and f.booking.ended_at is None
    assert f.payment.status == 'SUBMITTED' and f.payment.reconciliation_state == 'NOT_REQUIRED'
    assert PaymentEvent.query.count() == original_count
