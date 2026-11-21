"""Real row-lock tests using a disposable LOOPBACK PostgreSQL cluster.

Never reads DATABASE_URL, never uses an existing server, never runs migrations.
Skipped when PostgreSQL binaries are unavailable. No password/secret is needed.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

import psycopg
from __init__ import create_app
from extensions import db
from models import Appointment, User, XpEvent, utcnow
import attendance


class PostgresAttendanceConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable=shutil.which('initdb')
        if not executable and os.name=='nt':
            candidate=Path('C:/Program Files/PostgreSQL/18/bin/initdb.exe')
            executable=str(candidate) if candidate.exists() else None
        if not executable:
            raise unittest.SkipTest('PostgreSQL binaries unavailable; concurrency not verified')
        cls.bin=Path(executable).parent
        cls.flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        cls.temp=tempfile.TemporaryDirectory(prefix='learn2play-attendance-')
        cls.cluster=Path(cls.temp.name)/'cluster'
        cls.server=None
        cls.log=None
        try:
            result=subprocess.run([str(cls.bin/'initdb'),'-D',str(cls.cluster),'-U','l2_attendance_test','--auth=trust','--encoding=UTF8','--no-locale'],capture_output=True,timeout=120,creationflags=cls.flags)
            if result.returncode:
                raise RuntimeError('initdb failed for disposable cluster')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                cls.port=sock.getsockname()[1]
            cls.log=open(Path(cls.temp.name)/'postgres.log','w',encoding='utf-8')
            cls.server=subprocess.Popen([str(cls.bin/'postgres'),'-D',str(cls.cluster),'-h','127.0.0.1','-p',str(cls.port),'-F'],stdout=cls.log,stderr=cls.log,creationflags=cls.flags)
            started=False
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                try:
                    with psycopg.connect(host='127.0.0.1',port=cls.port,user='l2_attendance_test',dbname='postgres',connect_timeout=1,autocommit=True) as connection:
                        connection.execute('CREATE DATABASE attendance_test')
                    started=True
                    break
                except psycopg.OperationalError:
                    if cls.server.poll() is not None: break
                    time.sleep(.1)
            if not started:
                raise RuntimeError('Disposable PostgreSQL did not start on loopback')
            cls.uri=f'postgresql+psycopg://l2_attendance_test@127.0.0.1:{cls.port}/attendance_test'
        except Exception:
            cls.stop_cluster()
            raise

    @classmethod
    def stop_cluster(cls):
        if cls.server is not None:
            subprocess.run([str(cls.bin/'pg_ctl'),'-D',str(cls.cluster),'-m','immediate','-w','stop'],capture_output=True,timeout=30,creationflags=cls.flags)
            cls.server.wait(timeout=10)
        if cls.log: cls.log.close()
        # Only the directory this class just created may be recursively cleaned.
        root=Path(cls.temp.name).resolve()
        assert root.parent==Path(tempfile.gettempdir()).resolve() and root.name.startswith('learn2play-attendance-')
        cls.temp.cleanup()

    @classmethod
    def tearDownClass(cls): cls.stop_cluster()

    def setUp(self):
        self.app=create_app(dict(TESTING=True,SECRET_KEY='isolated-postgres-test-key',WTF_CSRF_ENABLED=False,SQLALCHEMY_DATABASE_URI=self.uri,SQLALCHEMY_ENGINE_OPTIONS={'connect_args':{'options':'-c lock_timeout=5000 -c statement_timeout=10000'}}))
        with self.app.app_context():
            db.create_all()
            student=User(display_name='Student',email='student@example.test',password_hash='test')
            provider=User(display_name='Provider',email='provider@example.test',password_hash='test',roles_json='["coach"]')
            db.session.add_all([student,provider])
            db.session.flush()
            appt=Appointment(student_id=student.id,provider_id=provider.id,role='coach',topic='Concurrent attendance',status='accepted',start_time=utcnow()-timedelta(hours=2),payment_required=False)
            db.session.add(appt)
            db.session.commit()
            self.student,self.provider,self.aid=student.id,provider.id,appt.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def call(self, uid, action):
        client=self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id']=str(uid)
            session['_fresh']=True
        return client.post(f'/api/bookings/{self.aid}/{action}',json={'note':'Attendance disagreement for concurrency test'})

    def test_simultaneous_confirmations_finalize_once(self):
        barrier=threading.Barrier(2)
        results=[]
        failures=[]
        def work(uid,action):
            try:
                barrier.wait(timeout=5)
                results.append(self.call(uid,action).status_code)
            except Exception as exc: failures.append(type(exc).__name__)
        threads=[threading.Thread(target=work,args=args) for args in ((self.student,'confirm-attendance'),(self.provider,'complete'))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(failures,failures)
        self.assertEqual(sorted(results),[200,200])
        with self.app.app_context():
            appt=db.session.get(Appointment,self.aid)
            self.assertEqual(appt.status,'completed')
            self.assertIsNotNone(appt.provider_confirmed_at)
            self.assertIsNotNone(appt.student_confirmed_at)
            self.assertEqual(XpEvent.query.count(),1)

    def run_ordered_race(self, first_action, first_actor, second_action, second_actor, pause_on):
        locked=threading.Event()
        release=threading.Event()
        results={}
        failures=[]
        original=attendance.audit
        def audit(*args,**kwargs):
            original(*args,**kwargs)
            if args[2]==pause_on:
                locked.set()
                if not release.wait(timeout=10): raise RuntimeError('Test lock release timed out')
        def work(key,uid,action):
            try: results[key]=self.call(uid,action).status_code
            except Exception as exc: failures.append(type(exc).__name__)
        with patch('attendance.audit',side_effect=audit):
            first=threading.Thread(target=work,args=('first',first_actor,first_action))
            second=threading.Thread(target=work,args=('second',second_actor,second_action))
            first.start()
            try:
                self.assertTrue(locked.wait(timeout=10))
                second.start()
                # Second request must not complete while the first holds the row.
                second.join(timeout=.25)
                self.assertTrue(second.is_alive())
            finally:
                release.set()
                first.join(timeout=15)
                if second.ident is not None: second.join(timeout=15)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(failures,failures)
        return results

    def test_committed_dispute_blocks_waiting_second_confirmation(self):
        self.assertEqual(self.call(self.student,'confirm-attendance').status_code,200)
        results=self.run_ordered_race('dispute-attendance',self.student,'complete',self.provider,'dispute_reported')
        self.assertEqual(results,{'first':200,'second':409})
        with self.app.app_context():
            self.assertTrue(db.session.get(Appointment,self.aid).attendance_disputed)
            self.assertEqual(XpEvent.query.count(),0)

    def test_finalization_first_rejects_late_dispute_without_inconsistent_state(self):
        self.assertEqual(self.call(self.student,'confirm-attendance').status_code,200)
        results=self.run_ordered_race('complete',self.provider,'dispute-attendance',self.student,'xp_awarded')
        self.assertEqual(results,{'first':200,'second':409})
        with self.app.app_context():
            appt=db.session.get(Appointment,self.aid)
            self.assertFalse(appt.attendance_disputed)
            self.assertEqual(appt.status,'completed')
            self.assertEqual(XpEvent.query.count(),1)


    def conflicting_transfer_fixture(self):
        from decimal import Decimal
        from payments import new_payment
        with self.app.app_context():
            admins=[User(display_name='Review Admin '+str(i),email=f'admin{i}@example.test',password_hash='test',is_admin=True) for i in range(2)]
            db.session.add_all(admins)
            payment_ids=[]
            for index in range(2):
                booking=Appointment(student_id=self.student,provider_id=self.provider,role='coach',topic='Transfer race',status='accepted',start_time=utcnow()+timedelta(days=index+1),price=Decimal('100.00'),payment_required=True)
                db.session.add(booking)
                db.session.flush()
                payment=new_payment(booking)
                payment.status='SUBMITTED'
                payment.method='GCash'
                payment.transaction_reference='  Shared-Race-123  ' if index==0 else 'shared-race-123'
                payment.receipt_path=f'test-only/{index}.png'
                db.session.add(payment)
                db.session.flush()
                payment_ids.append(payment.id)
            db.session.commit()
            return [user.id for user in admins],payment_ids

    def review_transfer(self, admin_id, payment_id):
        client=self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id']=str(admin_id)
            session['_fresh']=True
        return client.post(f'/api/admin/payments/{payment_id}/approve',json={'confirmed':True})

    def test_two_admins_cannot_approve_unresolved_duplicate_claims(self):
        from models import Payment
        admins,payment_ids=self.conflicting_transfer_fixture()
        barrier=threading.Barrier(2)
        results=[]
        failures=[]
        def review(index):
            try:
                barrier.wait(timeout=5)
                results.append(self.review_transfer(admins[index],payment_ids[index]).status_code)
            except Exception as exc:failures.append(type(exc).__name__)
        threads=[threading.Thread(target=review,args=(index,)) for index in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(failures,failures)
        self.assertEqual(sorted(results),[409,409])
        with self.app.app_context():
            self.assertEqual(Payment.query.filter_by(status='VERIFIED').count(),0)
            self.assertEqual(Payment.query.filter_by(status='SUBMITTED').count(),2)

    def test_unique_index_serializes_concurrent_reviews_even_with_stale_prechecks(self):
        from models import Payment
        admins,payment_ids=self.conflicting_transfer_fixture()
        barrier=threading.Barrier(2)
        results=[]
        failures=[]
        def stale_check(*args,**kwargs):
            # Force both reviewers past the application guard. The real database
            # index, not the patched precheck, must prevent double allocation.
            barrier.wait(timeout=5)
            return []
        def review(index):
            try:results.append(self.review_transfer(admins[index],payment_ids[index]).status_code)
            except Exception as exc:failures.append(type(exc).__name__)
        with patch('payments.transfer_claims',side_effect=stale_check):
            threads=[threading.Thread(target=review,args=(index,)) for index in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(failures,failures)
        self.assertEqual(sorted(results),[200,409])
        with self.app.app_context():
            self.assertEqual(Payment.query.filter_by(status='VERIFIED').count(),1)
            losing=Payment.query.filter_by(status='SUBMITTED').one()
            self.assertIsNone(losing.reviewed_by)
            self.assertIsNone(losing.reviewed_at)
            self.assertFalse(losing.appointment.payment_satisfied)
            from models import PaymentEvent
            self.assertEqual(PaymentEvent.query.filter_by(payment_id=losing.id).count(), 0)
            self.assertEqual(PaymentEvent.query.filter_by(event_type="VERIFIED").count(), 1)


    def booking_resources(self, cross_role=False):
        from decimal import Decimal
        from models import MentorAvailability
        with self.app.app_context():
            other_student=User(display_name='Other Learner',email='other-learner@example.test',password_hash='test')
            other_provider=User(display_name='Other Coach',email='other-coach@example.test',password_hash='test',roles_json='["coach"]',coaching_rate=Decimal('100.00'))
            db.session.add_all([other_student,other_provider])
            db.session.flush()
            provider=db.session.get(User,self.provider)
            provider.coaching_rate=Decimal('100.00')
            if cross_role:
                provider.roles=['student','coach']
                student=db.session.get(User,self.student)
                student.roles=['student','coach']
                student.coaching_rate=Decimal('100.00')
            self.booking_day=(utcnow()+timedelta(days=3)).date()
            for pid in ([self.provider,other_provider.id,self.student] if cross_role else [self.provider,other_provider.id]):
                db.session.add(MentorAvailability(mentor_id=pid,weekday=self.booking_day.weekday(),start_time='09:00',end_time='15:00'))
            db.session.commit()
            return other_student.id,other_provider.id

    def booking_request(self, student_id, provider_id, hour):
        client=self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id']=str(student_id)
            session['_fresh']=True
        return client.post('/api/bookings',json={'mentor_id':provider_id,'role':'coach','date':self.booking_day.isoformat(),'time':hour,'topic':'Concurrent reservation test'})

    def race_bookings(self, first_args, second_args, shared_schedule=True):
        import routes
        real_lock=routes.lock_schedules
        held=threading.Event()
        release=threading.Event()
        second_entered=threading.Event()
        second_done=threading.Event()
        results={}
        failures=[]
        backend_pids=set()
        def timed_lock(*args):
            # Observe separate real connections, then delegate to REAL SQL locks.
            backend_pids.add(db.session.connection().connection.driver_connection.info.backend_pid)
            first=threading.current_thread().name=='reservation-first'
            if not first:second_entered.set()
            users=real_lock(*args)
            if first:
                held.set()
                if not release.wait(timeout=10):raise RuntimeError('Test release timed out')
            return users
        def work(key,args):
            try:results[key]=self.booking_request(*args)
            except Exception as exc:failures.append(type(exc).__name__)
            finally:
                if key=='second':second_done.set()
        first=threading.Thread(name='reservation-first',target=work,args=('first',first_args))
        second=threading.Thread(name='reservation-second',target=work,args=('second',second_args))
        with patch('routes.lock_schedules',side_effect=timed_lock):
            first.start()
            try:
                self.assertTrue(held.wait(timeout=5))
                second.start()
                self.assertTrue(second_entered.wait(timeout=5))
                if shared_schedule:
                    self.assertFalse(second_done.wait(timeout=.25),'Second transaction did not wait on the shared schedule')
                else:
                    self.assertTrue(second_done.wait(timeout=5),'Disjoint participants should not share a reservation lock')
            finally:
                release.set()
                first.join(timeout=15)
                if second.ident is not None:second.join(timeout=15)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(failures,failures)
        self.assertEqual(len(backend_pids),2)
        return results['first'],results['second']

    def assert_reservation_counts(self, successes):
        from models import Payment
        with self.app.app_context():
            rows=Appointment.query.filter_by(topic='Concurrent reservation test').all()
            self.assertEqual(len(rows),successes)
            self.assertEqual(Payment.query.count(),successes)
            self.assertTrue(all(row.payment is not None and row.payment.status=='PENDING' for row in rows))

    def test_two_students_competing_for_one_provider_create_one_booking_and_payment(self):
        other,_=self.booking_resources()
        first,second=self.race_bookings((self.student,self.provider,'10:00'),(other,self.provider,'10:00'))
        self.assertEqual((first.status_code,second.status_code),(201,409))
        self.assertIn('choose another slot',second.json['message'])
        self.assert_reservation_counts(1)

    def test_one_student_competing_for_two_providers_cannot_overlap(self):
        _,other_provider=self.booking_resources()
        first,second=self.race_bookings((self.student,self.provider,'10:00'),(self.student,other_provider,'10:00'))
        self.assertEqual((first.status_code,second.status_code),(201,409))
        self.assert_reservation_counts(1)

    def test_nonoverlapping_bookings_succeed_after_waiting_for_shared_schedule(self):
        other,_=self.booking_resources()
        first,second=self.race_bookings((self.student,self.provider,'10:00'),(other,self.provider,'12:00'))
        self.assertEqual((first.status_code,second.status_code),(201,201))
        self.assert_reservation_counts(2)

    def test_adjacent_bookings_do_not_overlap(self):
        other,_=self.booking_resources()
        first,second=self.race_bookings((self.student,self.provider,'10:00'),(other,self.provider,'11:00'))
        self.assertEqual((first.status_code,second.status_code),(201,201))
        self.assert_reservation_counts(2)

    def test_disjoint_providers_and_students_book_and_accept_same_time_independently(self):
        other,other_provider=self.booking_resources()
        first,second=self.race_bookings((self.student,self.provider,'10:00'),(other,other_provider,'10:00'),shared_schedule=False)
        self.assertEqual((first.status_code,second.status_code),(201,201))
        for provider,response in ((self.provider,first),(other_provider,second)):
            client=self.app.test_client()
            with client.session_transaction() as session:session['_user_id']=str(provider);session['_fresh']=True
            self.assertEqual(client.post(f"/api/bookings/{response.json['booking_id']}/accept").status_code,200)
        self.assert_reservation_counts(2)

    def test_cross_role_bookings_share_person_schedule_and_lock_order(self):
        self.booking_resources(cross_role=True)
        first,second=self.race_bookings((self.student,self.provider,'10:00'),(self.provider,self.student,'10:00'))
        self.assertEqual((first.status_code,second.status_code),(201,409))
        self.assert_reservation_counts(1)

    def test_repeatable_read_reservations_fail_closed(self):
        from models import Payment
        self.booking_resources()
        previous_app=self.app
        self.app=create_app(dict(TESTING=True,SECRET_KEY='isolated-isolation-test',WTF_CSRF_ENABLED=False,SQLALCHEMY_DATABASE_URI=self.uri,SQLALCHEMY_ENGINE_OPTIONS={'isolation_level':'REPEATABLE READ'}))
        try:
            response=self.booking_request(self.student,self.provider,'10:00')
            self.assertEqual(response.status_code,503)
            with self.app.app_context():
                self.assertEqual(Appointment.query.filter_by(topic='Concurrent reservation test').count(),0)
                self.assertEqual(Payment.query.count(),0)
        finally:
            with previous_app.app_context():db.engine.dispose()

    def receipt_fixture(self):
        from payments import new_payment
        with self.app.app_context():
            appointment = db.session.get(Appointment, self.aid)
            appointment.payment_required = True
            appointment.price = 100
            db.session.add(new_payment(appointment))
            db.session.commit()

    def receipt_request(self):
        from io import BytesIO
        from tests.test_payments import PNG
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.student)
            session["_fresh"] = True
        return client.post(f"/api/payments/bookings/{self.aid}/submit", data={
            "receipt": (BytesIO(PNG), "receipt.png"), "method": "GCash", "transaction_reference": "TEST-RECEIPT"})

    def test_simultaneous_receipt_uploads_commit_only_one(self):
        from unittest.mock import MagicMock
        from models import Payment
        self.receipt_fixture()
        barrier = threading.Barrier(2)
        storage = MagicMock()
        results, failures = [], []
        def upload(*args, **kwargs):
            self.assertFalse(db.session().in_transaction())
            barrier.wait(timeout=10)
        storage.upload.side_effect = upload
        def work():
            try:
                results.append(self.receipt_request().status_code)
            except Exception as exc:
                failures.append(repr(exc))
        with patch("payments.receipts_bucket", return_value=storage):
            threads = [threading.Thread(target=work) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(failures, failures)
        self.assertEqual(sorted(results), [200, 409])
        with self.app.app_context():
            payment = Payment.query.one()
            self.assertEqual(payment.status, "SUBMITTED")
            winning_path = payment.receipt_path
            from models import PaymentEvent
            self.assertEqual(payment.submission_revision, 1)
            self.assertEqual(PaymentEvent.query.filter_by(payment_id=payment.id, revision=1).count(), 1)
        uploaded = {call.args[0] for call in storage.upload.call_args_list}
        storage.remove.assert_called_once_with(list(uploaded - {winning_path}))

    def test_cancellation_is_not_blocked_by_slow_receipt_upload(self):
        from unittest.mock import MagicMock
        from models import Payment
        self.receipt_fixture()
        uploading, release = threading.Event(), threading.Event()
        storage = MagicMock()
        results, failures = [], []
        def upload(*args, **kwargs):
            self.assertFalse(db.session().in_transaction())
            uploading.set()
            self.assertTrue(release.wait(timeout=15))
        storage.upload.side_effect = upload
        def work():
            try:
                results.append(self.receipt_request().status_code)
            except Exception as exc:
                failures.append(repr(exc))
        with patch("payments.receipts_bucket", return_value=storage):
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(uploading.wait(timeout=10))
                client = self.app.test_client()
                with client.session_transaction() as session:
                    session["_user_id"] = str(self.student)
                    session["_fresh"] = True
                self.assertEqual(client.post(f"/api/bookings/{self.aid}/cancel").status_code, 200)
            finally:
                release.set()
                thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(results, [409])
        with self.app.app_context():
            payment = Payment.query.one()
            self.assertEqual(payment.status, "PENDING")
            self.assertIsNone(payment.receipt_path)
            self.assertEqual(db.session.get(Appointment, self.aid).status, "cancelled")
        storage.remove.assert_called_once()

    def test_leaderboard_private_progress_uses_postgres_window_ranking(self):
        with self.app.app_context():
            student = db.session.get(User, self.student)
            student.leaderboard_opt_in = True
            appointment = db.session.get(Appointment, self.aid)
            appointment.status = "completed"
            db.session.add(XpEvent(user_id=self.student, appointment_id=self.aid,
                                  path="gaming", points=50))
            db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.student)
            session["_fresh"] = True
        response = client.get("/api/leaderboard/weekly")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["your_progress"], {"weekly_xp": 50, "rank": 1})
        self.assertEqual(response.json["entries"], [{"rank": 1, "display_name": "Student", "weekly_xp": 50}])

    def test_simultaneous_resubmissions_preserve_revision_history(self):
        from unittest.mock import MagicMock
        from models import Payment, PaymentEvent
        self.receipt_fixture()
        storage = MagicMock()
        with self.app.app_context():
            admin = User(display_name="History Admin", email="history-admin@example.test", password_hash="test", is_admin=True)
            db.session.add(admin)
            db.session.commit()
            admin_id = admin.id
        with patch("payments.receipts_bucket", return_value=storage):
            self.assertEqual(self.receipt_request().status_code, 200)
            with self.app.app_context():
                payment_id = Payment.query.one().id
                previous_path = Payment.query.one().receipt_path
            client = self.app.test_client()
            with client.session_transaction() as session:
                session["_user_id"] = str(admin_id)
                session["_fresh"] = True
            self.assertEqual(client.post(f"/api/admin/payments/{payment_id}/reject", json={"reason":"Transfer not found"}).status_code, 200)
            barrier = threading.Barrier(2)
            results, failures = [], []
            def upload(*args, **kwargs):
                self.assertFalse(db.session().in_transaction())
                barrier.wait(timeout=10)
            storage.upload.side_effect = upload
            def work():
                try:
                    results.append(self.receipt_request().status_code)
                except Exception as exc:
                    failures.append(repr(exc))
            threads = [threading.Thread(target=work) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(failures, failures)
        self.assertEqual(sorted(results), [200, 409])
        with self.app.app_context():
            payment = db.session.get(Payment, payment_id)
            self.assertEqual(payment.submission_revision, 2)
            events = PaymentEvent.query.filter_by(payment_id=payment_id).order_by(PaymentEvent.id).all()
            self.assertEqual([(e.event_type,e.revision) for e in events], [("SUBMITTED",1),("REJECTED",1),("RESUBMITTED",2)])
            self.assertEqual(events[0].receipt_path, previous_path)
            self.assertEqual(events[1].receipt_path, previous_path)
            self.assertEqual(events[2].receipt_path, payment.receipt_path)
            self.assertEqual(events[1].reviewer_id, admin_id)
        self.assertEqual(storage.remove.call_count, 1)
        self.assertNotIn(previous_path, storage.remove.call_args.args[0])

    def test_postgres_audit_rejects_update_delete_and_truncate(self):
        from unittest.mock import MagicMock
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        from models import PaymentEvent
        self.receipt_fixture()
        with patch("payments.receipts_bucket", return_value=MagicMock()):
            self.assertEqual(self.receipt_request().status_code, 200)
        with self.app.app_context():
            for statement in ("UPDATE payment_events SET new_status='VERIFIED'", "DELETE FROM payment_events", "TRUNCATE payment_events", "TRUNCATE payments CASCADE"):
                with self.assertRaises(DBAPIError):
                    db.session.execute(text(statement))
                    db.session.commit()
                db.session.rollback()
            self.assertEqual(PaymentEvent.query.count(), 1)
            self.assertEqual(PaymentEvent.query.one().new_status, "SUBMITTED")

    def test_concurrent_reviews_of_one_payment_append_one_decision(self):
        from unittest.mock import MagicMock
        from models import Payment, PaymentEvent
        self.receipt_fixture()
        with patch("payments.receipts_bucket", return_value=MagicMock()):
            self.assertEqual(self.receipt_request().status_code, 200)
        with self.app.app_context():
            admin = User(display_name="Reviewer", email="reviewer@example.test", password_hash="test", is_admin=True)
            db.session.add(admin)
            db.session.commit()
            admin_id = admin.id
            payment_id = Payment.query.one().id
        barrier = threading.Barrier(2)
        results, failures = [], []
        def work():
            try:
                client = self.app.test_client()
                with client.session_transaction() as session:
                    session["_user_id"] = str(admin_id)
                    session["_fresh"] = True
                barrier.wait(timeout=10)
                results.append(client.post(f"/api/admin/payments/{payment_id}/approve", json={"confirmed":True}).status_code)
            except Exception as exc:
                failures.append(repr(exc))
        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(failures, failures)
        self.assertEqual(sorted(results), [200, 409])
        with self.app.app_context():
            events = PaymentEvent.query.filter_by(payment_id=payment_id).order_by(PaymentEvent.id).all()
            self.assertEqual([(e.event_type,e.revision) for e in events], [("SUBMITTED",1),("VERIFIED",1)])

    def reconciliation_case(self):
        from unittest.mock import MagicMock
        from models import Payment
        self.receipt_fixture()
        with patch("payments.receipts_bucket", return_value=MagicMock()):
            self.assertEqual(self.receipt_request().status_code, 200)
        with self.app.app_context():
            admins = [User(display_name=f"Financial Recorder {i}", email=f"financial-recorder-{i}@example.test", password_hash="test", is_admin=True) for i in range(2)]
            db.session.add_all(admins)
            db.session.commit()
            return Payment.query.one().id, [admin.id for admin in admins]

    def financial_request(self, uid, path, data):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(uid)
            session["_fresh"] = True
        return client.post(path, json=data)

    def run_financial_lock_race(self, first, second, held_event_type):
        from payments import record_event
        holding, release = threading.Event(), threading.Event()
        results, failures = {}, []
        def audit(*args, **kwargs):
            result = record_event(*args, **kwargs)
            if args[1] == held_event_type:
                holding.set()
                self.assertTrue(release.wait(timeout=15))
            return result
        def work(key, spec):
            try:
                results[key] = self.financial_request(*spec).status_code
            except Exception as exc:
                failures.append(repr(exc))
        with patch("payments.record_event", side_effect=audit):
            first_thread = threading.Thread(target=work,args=('first',first))
            second_thread = threading.Thread(target=work,args=('second',second))
            first_thread.start()
            try:
                self.assertTrue(holding.wait(timeout=10))
                second_thread.start()
                second_thread.join(timeout=.25)
                self.assertTrue(second_thread.is_alive())
            finally:
                release.set()
                first_thread.join(timeout=20)
                if second_thread.ident is not None: second_thread.join(timeout=20)
        self.assertFalse(first_thread.is_alive() or second_thread.is_alive())
        self.assertFalse(failures,failures)
        return results

    def test_cancellation_wins_waiting_payment_approval_without_financial_history_loss(self):
        from models import Payment, PaymentEvent
        pid, admins = self.reconciliation_case()
        cancel = (self.student,f"/api/bookings/{self.aid}/cancel",{'reason':'Demo participant cancellation'})
        approve = (admins[0],f"/api/admin/payments/{pid}/approve",{'confirmed':True})
        result = self.run_financial_lock_race(cancel,approve,'BOOKING_ENDED')
        self.assertEqual(result,{'first':200,'second':409})
        with self.app.app_context():
            payment = db.session.get(Payment,pid)
            self.assertEqual((payment.status,payment.reconciliation_state),('SUBMITTED','NEEDS_REVIEW'))
            self.assertEqual([e.event_type for e in payment.events],['SUBMITTED','BOOKING_ENDED'])
            self.assertEqual(XpEvent.query.count(),0)

    def test_approval_wins_then_waiting_cancellation_keeps_verification_and_flags_review(self):
        from models import Payment
        pid, admins = self.reconciliation_case()
        approve = (admins[0],f"/api/admin/payments/{pid}/approve",{'confirmed':True})
        cancel = (self.student,f"/api/bookings/{self.aid}/cancel",{'reason':'Demo participant cancellation'})
        result = self.run_financial_lock_race(approve,cancel,'VERIFIED')
        self.assertEqual(result,{'first':200,'second':200})
        with self.app.app_context():
            payment = db.session.get(Payment,pid)
            self.assertEqual((payment.status,payment.reconciliation_state),('VERIFIED','NEEDS_REVIEW'))
            self.assertEqual(payment.reviewed_by,admins[0])
            self.assertIsNotNone(payment.reviewed_at)
            self.assertEqual([e.event_type for e in payment.events],['SUBMITTED','VERIFIED','BOOKING_ENDED'])
            self.assertEqual(XpEvent.query.count(),0)

    def test_concurrent_reconciliation_decisions_accept_only_fresh_authorized_record(self):
        from models import Payment, PaymentEvent
        pid, admins = self.reconciliation_case()
        self.assertEqual(self.financial_request(self.student,f"/api/bookings/{self.aid}/cancel",{'reason':'Demo cancellation'}).status_code,200)
        common = dict(expected_version=1,reason='Demo internal authorized financial decision',
                      student_explanation='Demo public explanation of the external financial outcome',
                      confirmation_notes='DEMO confirmation supplied by the external authorized decision-maker',
                      evidence_reference='DEMO-NO-REAL-MONEY',authority_name='DEMO external account holder',
                      authority_capacity='account_holder',funds_received_confirmed=True)
        retain = dict(common,state='RETAINED',authorized_retention_confirmed=True)
        refund = dict(common,state='REFUND_PENDING',authorized_refund_confirmed=True)
        result = self.run_financial_lock_race(
            (admins[0],f"/api/admin/payments/{pid}/reconcile",retain),
            (admins[1],f"/api/admin/payments/{pid}/reconcile",refund),'RETAINED')
        self.assertEqual(result,{'first':200,'second':409})
        with self.app.app_context():
            payment = db.session.get(Payment,pid)
            self.assertEqual((payment.status,payment.reconciliation_state,payment.reconciliation_version),('SUBMITTED','RETAINED',2))
            self.assertEqual([e.event_type for e in payment.events],['SUBMITTED','BOOKING_ENDED','RETAINED'])
            event = PaymentEvent.query.filter_by(payment_id=pid,reconciliation_version=2).one()
            self.assertEqual(event.actor_id,admins[0])
            self.assertTrue(event.authorized_retention_confirmed)
            self.assertEqual(XpEvent.query.count(),0)


if __name__=='__main__': unittest.main()
