import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from __init__ import create_app
from extensions import db
from models import Appointment, AttendanceEvent, Payment, User, XpEvent, utcnow
from payments import new_payment


class AttendanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(dict(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY='attendance-test-key', SQLALCHEMY_DATABASE_URI='sqlite://', SQLALCHEMY_ENGINE_OPTIONS={}))
        with self.app.app_context():
            db.create_all()
            users = [User(display_name=name, email=name+'@example.test', password_hash='test', roles_json=roles, is_admin=admin) for name,roles,admin in (
                ('Learner','["student"]',False),('Teacher','["mentor"]',False),('Coach','["coach"]',False),('Other','["student"]',False),('Admin','["student"]',True))]
            db.session.add_all(users)
            db.session.commit()
            self.student,self.mentor,self.coach,self.other,self.admin = [u.id for u in users]

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def client(self, uid):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True
        return client

    def booking(self, role='mentor', payment=None, status='accepted', future=False):
        with self.app.app_context():
            appt = Appointment(student_id=self.student, provider_id=self.mentor if role=='mentor' else self.coach, role=role, topic='Attendance', start_time=utcnow()+timedelta(hours=2) if future else utcnow()-timedelta(hours=2), price=Decimal('100.00'), status=status, payment_required=payment is not None)
            db.session.add(appt)
            db.session.flush()
            if payment is not None:
                record=new_payment(appt)
                record.status=payment
                db.session.add(record)
            db.session.commit()
            return appt.id

    def act(self, uid, aid, action, **data):
        return self.client(uid).post(f'/api/bookings/{aid}/{action}',json=data)

    def resolve(self, aid, outcome, reason='Independent dispute review completed'):
        return self.client(self.admin).post(f'/api/admin/attendance/{aid}/resolve',json=dict(outcome=outcome,reason=reason))

    def test_both_confirmation_orders_and_provider_tracks_award_exactly_once(self):
        for role,provider in [('mentor',self.mentor),('coach',self.coach)]:
            for reverse in (False,True):
                with self.subTest(role=role,reverse=reverse):
                    aid=self.booking(role=role)
                    actions=[(provider,'complete'),(self.student,'confirm-attendance')]
                    if reverse: actions.reverse()
                    self.assertEqual(self.act(*[actions[0][0],aid,actions[0][1]]).json['status'],'accepted')
                    with self.app.app_context():
                        self.assertEqual(XpEvent.query.filter_by(appointment_id=aid).count(),0)
                    self.assertEqual(self.act(actions[1][0],aid,actions[1][1]).json['status'],'completed')
                    for uid,action in actions:
                        self.assertEqual(self.act(uid,aid,action).status_code,200)
                    with self.app.app_context():
                        award=XpEvent.query.filter_by(appointment_id=aid).one()
                        self.assertEqual((award.user_id,award.points,award.path),(self.student,50,'academic' if role=='mentor' else 'gaming'))
                        self.assertEqual(AttendanceEvent.query.filter_by(appointment_id=aid).count(),3)

    def test_unverified_paid_bookings_cannot_confirm_or_award(self):
        for state in ('PENDING','SUBMITTED','REJECTED'):
            aid=self.booking(payment=state)
            for uid,action in ((self.student,'confirm-attendance'),(self.mentor,'complete')):
                self.assertEqual(self.act(uid,aid,action).status_code,403)
            with self.app.app_context():
                appt=db.session.get(Appointment,aid)
                self.assertIsNone(appt.student_confirmed_at)
                self.assertIsNone(appt.provider_confirmed_at)
                self.assertEqual(XpEvent.query.filter_by(appointment_id=aid).count(),0)
        aid=self.booking(payment='VERIFIED')
        self.act(self.student,aid,'confirm-attendance')
        self.assertEqual(self.act(self.mentor,aid,'complete').json['status'],'completed')

    def test_dispute_is_allowed_without_payment_and_records_reporter_reason(self):
        aid=self.booking(payment='PENDING')
        self.assertEqual(self.act(self.student,aid,'dispute-attendance',note='').status_code,400)
        self.assertEqual(self.act(self.student,aid,'dispute-attendance',note='Educator did not attend').status_code,200)
        self.assertEqual(self.act(self.mentor,aid,'complete').status_code,409)
        self.assertEqual(self.act(self.student,aid,'dispute-attendance',note='Educator did not attend').status_code,200)
        with self.app.app_context():
            appt=db.session.get(Appointment,aid)
            self.assertTrue(appt.attendance_disputed)
            self.assertEqual(appt.attendance_disputed_by_id,self.student)
            event=AttendanceEvent.query.filter_by(appointment_id=aid).one()
            self.assertEqual((event.actor_id,event.note),(self.student,'Educator did not attend'))
        page=self.client(self.admin).get('/admin/attendance')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'Educator did not attend',page.data)

    def test_admin_reopens_without_awarding_and_both_must_reconfirm(self):
        aid=self.booking(payment='VERIFIED')
        self.act(self.mentor,aid,'complete')
        self.act(self.student,aid,'dispute-attendance',note='Attendance was interrupted')
        self.assertEqual(self.resolve(aid,'award').status_code,400)
        self.assertEqual(self.resolve(aid,'reopen',reason='').status_code,400)
        self.assertEqual(self.resolve(aid,'reopen').status_code,200)
        self.assertEqual(self.resolve(aid,'reopen').status_code,409)
        with self.app.app_context():
            appt=db.session.get(Appointment,aid)
            self.assertFalse(appt.attendance_disputed)
            self.assertIsNotNone(appt.attendance_disputed_at)
            self.assertEqual(appt.attendance_resolved_by_id,self.admin)
            self.assertIsNotNone(appt.attendance_resolved_at)
            self.assertIsNone(appt.provider_confirmed_at)
            self.assertIsNone(appt.student_confirmed_at)
            self.assertEqual(XpEvent.query.count(),0)
            self.assertEqual([e.action for e in AttendanceEvent.query.order_by(AttendanceEvent.id)],['provider_confirmed','dispute_reported','dispute_reopen'])
        self.act(self.student,aid,'confirm-attendance')
        with self.app.app_context(): self.assertEqual(XpEvent.query.count(),0)
        self.assertEqual(self.act(self.mentor,aid,'complete').json['status'],'completed')

    def test_admin_closes_without_xp_and_preserves_history(self):
        aid=self.booking()
        self.act(self.student,aid,'confirm-attendance')
        self.act(self.mentor,aid,'dispute-attendance',note='Session did not occur')
        self.assertEqual(self.resolve(aid,'close_without_xp').status_code,200)
        for uid,action in ((self.student,'confirm-attendance'),(self.mentor,'complete'),(self.student,'dispute-attendance')):
            self.assertEqual(self.act(uid,aid,action,note='Try reporting again').status_code,409)
        with self.app.app_context():
            appt=db.session.get(Appointment,aid)
            self.assertIsNotNone(appt.attendance_closed_at)
            self.assertFalse(appt.attendance_disputed)
            self.assertEqual(XpEvent.query.count(),0)
            self.assertEqual(AttendanceEvent.query.count(),3)
        self.assertIn(b'close_without_xp',self.client(self.admin).get('/admin/attendance?state=resolved').data)

    def test_only_assigned_users_confirm_and_only_admin_resolves(self):
        aid=self.booking()
        for uid,action in ((self.other,'confirm-attendance'),(self.other,'complete'),(self.other,'dispute-attendance'),(self.mentor,'confirm-attendance'),(self.student,'complete'),(self.admin,'complete')):
            self.assertEqual(self.act(uid,aid,action,note='Unauthorized').status_code,403)
        for uid in (self.student,self.mentor,self.coach,self.other):
            self.assertEqual(self.client(uid).get('/admin/attendance').status_code,403)
            self.assertEqual(self.client(uid).post(f'/api/admin/attendance/{aid}/resolve',json=dict(outcome='reopen',reason='Unauthorized')).status_code,403)

    def test_early_cancelled_and_legacy_completed_sessions(self):
        future=self.booking(future=True)
        self.assertEqual(self.act(self.student,future,'confirm-attendance').status_code,400)
        aid=self.booking()
        self.act(self.student,aid,'confirm-attendance')
        self.assertEqual(self.act(self.mentor,aid,'cancel').status_code,200)
        self.assertEqual(self.act(self.mentor,aid,'complete').status_code,403)
        legacy=self.booking(status='completed')
        self.assertEqual(self.act(self.student,legacy,'confirm-attendance').status_code,200)
        self.assertEqual(self.act(self.mentor,legacy,'complete').status_code,200)
        with self.app.app_context():
            self.assertEqual(XpEvent.query.count(),0)
            self.assertIsNone(db.session.get(Appointment,legacy).student_confirmed_at)

    def test_transaction_failure_rolls_back_confirmation_award_and_audit(self):
        from sqlalchemy.exc import SQLAlchemyError
        aid=self.booking()
        self.act(self.student,aid,'confirm-attendance')
        with patch.object(db.session,'commit',side_effect=SQLAlchemyError('isolated failure')):
            self.assertEqual(self.act(self.mentor,aid,'complete').status_code,503)
        with self.app.app_context():
            appt=db.session.get(Appointment,aid)
            self.assertEqual(appt.status,'accepted')
            self.assertIsNone(appt.provider_confirmed_at)
            self.assertEqual(XpEvent.query.count(),0)
            self.assertEqual(AttendanceEvent.query.count(),1)

    def test_csrf_required_and_valid_token_works(self):
        aid=self.booking()
        self.app.config['WTF_CSRF_ENABLED']=True
        client=self.client(self.student)
        path=f'/api/bookings/{aid}/confirm-attendance'
        self.assertEqual(client.post(path).status_code,400)
        token=client.get('/api/csrf-token').json['csrf_token']
        self.assertEqual(client.post(path,headers={'X-CSRFToken':token}).status_code,200)
        self.assertEqual(self.client(self.admin).post(f'/api/admin/attendance/{aid}/resolve',json=dict(outcome='reopen',reason='No CSRF')).status_code,400)

    def test_user_cannot_submit_xp_or_level_and_self_attendance_is_rejected(self):
        aid=self.booking()
        self.act(self.student,aid,'confirm-attendance',points=999999,level=99,user_id=self.other)
        self.act(self.mentor,aid,'complete',points=999999,level=99)
        progress=self.client(self.student).get('/api/student/progress')
        self.assertEqual((progress.json['total_xp'],progress.json['level']),(50,1))
        self.assertEqual(self.client(self.student).post('/api/student/progress',json=dict(total_xp=9999)).status_code,405)
        with self.app.app_context():
            appt=db.session.get(Appointment,aid)
            appt.provider_id=self.student
            appt.status='accepted'
            db.session.commit()
        self.assertEqual(self.act(self.student,aid,'complete').status_code,403)

    def test_new_dispute_after_reopen_retains_prior_decision(self):
        aid=self.booking()
        self.act(self.student,aid,'dispute-attendance',note='First disagreement')
        self.resolve(aid,'reopen')
        self.act(self.mentor,aid,'dispute-attendance',note='Second disagreement')
        with self.app.app_context():
            appt=db.session.get(Appointment,aid)
            self.assertTrue(appt.attendance_disputed)
            self.assertIsNone(appt.attendance_resolved_at)
            self.assertEqual(AttendanceEvent.query.filter_by(appointment_id=aid).count(),3)

    def test_cancelled_dispute_can_close_but_cannot_reopen(self):
        aid=self.booking()
        self.act(self.student,aid,'dispute-attendance',note='Attendance did not happen')
        self.act(self.student,aid,'cancel')
        self.assertEqual(self.resolve(aid,'reopen').status_code,409)
        self.assertEqual(self.resolve(aid,'close_without_xp').status_code,200)


    def test_backend_progress_levels_badges_quests_and_week_boundaries(self):
        from datetime import datetime, timezone
        clock=datetime(2026,11,18,8,tzinfo=timezone.utc)
        # Monday midnight PHT, stored as naive UTC.
        monday=datetime(2026,11,15,16)
        with self.app.app_context():
            for index in range(20):
                path='academic' if index%2==0 else 'gaming'
                created=monday+timedelta(hours=index) if index<5 else monday-timedelta(days=7) if index<7 else monday-timedelta(days=28)
                appt=Appointment(student_id=self.student,provider_id=self.mentor,topic='Recorded session',status='completed',start_time=created-timedelta(hours=2))
                db.session.add(appt)
                db.session.flush()
                db.session.add(XpEvent(user_id=self.student,appointment_id=appt.id,path=path,points=50,created_at=created))
            db.session.commit()
        with patch('routes.datetime',wraps=datetime) as mocked:
            mocked.now.return_value=clock
            response=self.client(self.student).get('/api/student/progress')
            other=self.client(self.other).get('/api/student/progress')
        self.assertEqual(response.status_code,200)
        data=response.json
        self.assertEqual((data['total_xp'],data['academic_xp'],data['gaming_xp']),(1000,500,500))
        self.assertEqual((data['level'],data['level_name']),(5,'Elite Student Gamer'))
        self.assertEqual(data['badges'],dict(academic=True,gaming=True,combined=True))
        self.assertEqual(data['quests']['academic_week'],dict(count=3,target=3))
        self.assertEqual(data['quests']['gaming_week'],dict(count=2,target=2))
        self.assertEqual(data['balance_streak_weeks'],2)
        self.assertEqual(data['activity_days'],['2026-11-16'])
        self.assertEqual(other.json['total_xp'],0)
        self.assertIn('no-store',response.headers['Cache-Control'])

    def test_leaderboard_preference_ignores_other_user_and_invalid_values(self):
        client=self.client(self.student)
        for invalid in ([], {'opted_in':'true'}, {'opted_in':1}):
            self.assertEqual(client.post('/api/student/leaderboard-preference',json=invalid).status_code,400)
        self.assertEqual(client.post('/api/student/leaderboard-preference',json={'opted_in':True,'user_id':self.other}).status_code,200)
        with self.app.app_context():
            self.assertTrue(db.session.get(User,self.student).leaderboard_opt_in)
            self.assertFalse(db.session.get(User,self.other).leaderboard_opt_in)
        for uid in (self.mentor,self.coach,self.admin):
            self.assertEqual(self.client(uid).get('/api/leaderboard/weekly').status_code,403)

    def test_self_booking_is_rejected_by_backend(self):
        with self.app.app_context():
            user=db.session.get(User,self.student)
            user.roles=['student','mentor']
            db.session.commit()
        day=(utcnow()+timedelta(days=2)).date().isoformat()
        response=self.client(self.student).post('/api/bookings',json={'mentor_id':self.student,'date':day,'time':'10:00','topic':'Self award attempt','role':'mentor'})
        self.assertEqual(response.status_code,403)


if __name__=='__main__': unittest.main()
