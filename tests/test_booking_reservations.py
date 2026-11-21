"""Reservation behavior and atomic appointment/payment failure tests."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import Appointment, MentorAvailability, MentorUnavailableDate, Payment, User, utcnow


@pytest.fixture
def booking_fixture():
    from test_payments import PaymentTests
    f=PaymentTests(methodName='runTest')
    f.setUp()
    try:
        f.day=(utcnow()+timedelta(days=4)).date()
        db.session.add(MentorAvailability(mentor_id=f.provider.id,weekday=f.day.weekday(),start_time='09:00',end_time='15:00'))
        db.session.commit()
        yield f
    finally:f.tearDown()


def book(f,hour='10:00',provider=None,student=None):
    f.login(student or f.student)
    return f.client.post('/api/bookings',json={'mentor_id':(provider or f.provider).id,'role':'mentor','date':f.day.isoformat(),'time':hour,'topic':'Booking regression'})


def existing(f,status='pending',hour=10,duration=60,provider=None,student=None):
    row=Appointment(provider_id=(provider or f.provider).id,student_id=(student or f.other).id,topic='Existing reservation',status=status,start_time=datetime.combine(f.day,datetime.min.time())+timedelta(hours=hour-8),duration_minutes=duration)
    db.session.add(row);db.session.commit()
    return row


@pytest.mark.parametrize('status',['pending','accepted','scheduled'])
def test_reserving_provider_statuses_prevent_booking_and_payment(booking_fixture,status):
    f=booking_fixture
    existing(f,status=status)
    before=(Appointment.query.count(),Payment.query.count())
    assert book(f).status_code==409
    assert (Appointment.query.count(),Payment.query.count())==before


@pytest.mark.parametrize('status',['cancelled','rejected','completed'])
def test_nonreserving_statuses_allow_new_bookings_without_deleting_history(booking_fixture,status):
    f=booking_fixture
    old=existing(f,status=status)
    response=book(f)
    assert response.status_code==201
    row=db.session.get(Appointment,response.json['booking_id'])
    assert row.duration_minutes==60 and row.payment.amount==Decimal('123.45')
    assert old.status==status


def test_student_conflict_with_different_provider_is_rejected(booking_fixture):
    f=booking_fixture
    other=User(display_name='Other Mentor',email='other-mentor@example.test',password_hash='test',roles_json='["mentor"]')
    db.session.add(other);db.session.commit()
    existing(f,provider=other,student=f.student)
    before=Payment.query.count()
    assert book(f).status_code==409
    assert Payment.query.count()==before


def test_adjacent_and_nonoverlapping_bookings_are_allowed(booking_fixture):
    f=booking_fixture
    existing(f)
    assert book(f,hour='11:00').status_code==201
    assert book(f,hour='13:00').status_code==201


def test_existing_longer_duration_is_respected(booking_fixture):
    f=booking_fixture
    existing(f,duration=90)
    assert book(f,hour='11:00').status_code==409
    assert book(f,hour='12:00').status_code==201


@pytest.mark.parametrize('swap',['student_is_teaching','provider_is_learning'])
def test_teaching_and_learning_share_the_same_person_schedule(booking_fixture,swap):
    f=booking_fixture
    outsider=User(display_name='Other participant',email='outsider@example.test',password_hash='test',roles_json='["mentor","student"]')
    db.session.add(outsider);db.session.commit()
    if swap=='student_is_teaching':existing(f,provider=f.student,student=outsider)
    else:existing(f,provider=outsider,student=f.provider)
    assert book(f).status_code==409
    f.login(f.student)
    preview=f.client.get(f'/api/tutors/{f.provider.id}/availability?date={f.day.isoformat()}')
    assert '10:00' not in preview.json['slots']


@pytest.mark.parametrize('failed_table',['appointments','payments'])
def test_insert_failure_rolls_back_both_records_and_allows_retry(booking_fixture,failed_table):
    f=booking_fixture
    before=(Appointment.query.count(),Payment.query.count())
    def fail_insert(connection,cursor,statement,parameters,context,executemany):
        if statement.lower().startswith('insert into '+failed_table):
            raise SQLAlchemyError('Isolated insert failure')
    event.listen(db.engine,'before_cursor_execute',fail_insert)
    try:response=book(f)
    finally:event.remove(db.engine,'before_cursor_execute',fail_insert)
    assert response.status_code==503
    assert (Appointment.query.count(),Payment.query.count())==before
    assert book(f).status_code==201


def test_acceptance_rechecks_legacy_overlaps_before_confirming(booking_fixture):
    f=booking_fixture
    first=existing(f,student=f.student)
    second=existing(f,student=f.other)
    f.login(f.provider)
    before=Payment.query.count()
    assert f.client.post(f'/api/bookings/{first.id}/accept').status_code==409
    assert first.status=='pending' and not first.meeting_room
    assert f.client.post(f'/api/bookings/{second.id}/reject').status_code==200
    assert f.client.post(f'/api/bookings/{first.id}/accept').status_code==200
    assert first.status=='accepted' and first.meeting_room
    assert Payment.query.count()==before


def test_unavailable_dates_and_availability_changes_still_apply(booking_fixture):
    f=booking_fixture
    f.login(f.provider)
    result=f.client.post('/api/unavailable-dates',json={'date':f.day.isoformat()})
    assert result.status_code==201
    assert book(f).status_code==400
    f.login(f.provider)
    unavailable_id=MentorUnavailableDate.query.filter_by(mentor_id=f.provider.id,unavailable_date=f.day).one().id
    assert f.client.delete('/api/unavailable-dates',json={'id':unavailable_id}).status_code==200
    window=MentorAvailability.query.filter_by(mentor_id=f.provider.id).one()
    assert f.client.delete('/api/availability',json={'id':window.id}).status_code==200
    assert book(f).status_code==400
    f.login(f.provider)
    assert f.client.post('/api/availability',json={'weekday':f.day.weekday(),'start_time':'09:00','end_time':'15:00'}).status_code==201
    assert book(f).status_code==201


def test_free_bookings_have_no_new_payment(booking_fixture):
    f=booking_fixture
    f.provider.teaching_rate=Decimal('0.00');db.session.commit()
    before=Payment.query.count()
    response=book(f)
    assert response.status_code==201
    row=db.session.get(Appointment,response.json['booking_id'])
    assert row.price==Decimal('0.00') and not row.payment_required and row.payment is None
    assert Payment.query.count()==before


def test_slot_must_fit_wholly_inside_availability(booking_fixture):
    f=booking_fixture
    window=MentorAvailability.query.filter_by(mentor_id=f.provider.id).one()
    window.start_time='23:00';window.end_time='23:30';db.session.commit()
    assert book(f,hour='23:00').status_code==400


def test_invalid_timezone_suffix_and_nonhour_slot_are_rejected(booking_fixture):
    f=booking_fixture
    assert book(f,hour='10:00+08:00').status_code==400
    assert book(f,hour='10:30').status_code==400
