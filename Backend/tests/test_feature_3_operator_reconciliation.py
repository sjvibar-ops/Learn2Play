"""Hermetic operator review and recovery workflow tests."""
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
import sqlalchemy as sa
try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import User, Appointment, DiscordChannelRecovery
    from Backend import discord_reconciliation as recon
    from Backend import discord_oauth
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import User, Appointment, DiscordChannelRecovery
    import discord_reconciliation as recon
    import discord_oauth

class RecoveryOperatorTests(TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False,
                               'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:'})
        self.ctx = self.app.app_context(); self.ctx.push()
        self.assertEqual(db.engine.url.database, ':memory:')
        db.create_all()
        self.admin = User(display_name='Admin', email='admin@review.test', password_hash='x', is_admin=True)
        self.nonadmin = User(display_name='Other', email='normal@review.test', password_hash='x')
        db.session.add_all((self.admin,self.nonadmin)); db.session.flush()
        self.aid_admin, self.aid_other = self.admin.id,self.nonadmin.id
        self.booking = Appointment(student_id=self.aid_other,provider_id=self.aid_admin,
            role='mentor', topic='Review',status='cancelled',start_time=datetime(2026,12,1))
        db.session.add(self.booking);db.session.flush()
        self.record = DiscordChannelRecovery(event_key='terminal:review:1',appointment_id=self.booking.id,
            channel_id='123456789012345678',recovery_reason='CANCELLED',recovery_status='PENDING')
        db.session.add(self.record);db.session.commit()
        self.rid=self.record.id
        self.client=self.app.test_client()
    def tearDown(self):
        db.session.remove();db.drop_all();self.ctx.pop()
    def login(self,which):
        with self.client.session_transaction() as s:
            s['_user_id']=str(which);s['_fresh']=True
        from flask import g
        g.__dict__.pop('_login_user', None)
    def test_listing_requires_admin(self):
        self.assertIn(self.client.get('/api/admin/discord/recoveries').status_code,(302,401))
        self.login(self.aid_other)
        self.assertEqual(self.client.get('/api/admin/discord/recoveries').status_code,403)
    def test_admin_can_inspect(self):
        self.login(self.aid_admin)
        r=self.client.get('/api/admin/discord/recoveries')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['recoveries'][0]['id'],self.rid)
        self.assertTrue(r.json['recoveries'][0]['requires_manual_review'])
        self.assertEqual(self.client.get(f'/api/admin/discord/recoveries/{self.rid}').status_code,200)
    def test_bad_pagination_fails(self):
        self.login(self.aid_admin)
        self.assertEqual(self.client.get('/api/admin/discord/recoveries?limit=10000').status_code,400)
    def test_flag_has_durable_audit(self):
        result=recon.decide(self.rid,self.aid_admin,0,'FLAG','Needs Discord-side inspection')
        self.assertEqual(result.status,recon.Decision.FLAGGED)
        db.session.expire_all()
        r=db.session.get(DiscordChannelRecovery,self.rid)
        self.assertEqual(r.recovery_status,'NEEDS_MANUAL_REVIEW')
        self.assertEqual(r.review_version,1)
        self.assertEqual(r.operator_id,self.aid_admin)
        self.assertIsNone(r.resolved_at)
    def test_stale_review_fenced(self):
        recon.decide(self.rid,self.aid_admin,0,'FLAG','Needs Discord-side inspection')
        self.assertEqual(recon.decide(self.rid,self.aid_admin,0,'RESOLVE','Verified externally by operator').status,recon.Decision.STALE)
    def test_resolution_does_not_delete_or_unlock(self):
        with patch.object(discord_oauth,'delete_voice_channel',side_effect=AssertionError('Discord called')):
            self.assertEqual(recon.decide(self.rid,self.aid_admin,0,'RESOLVE','Confirmed manually in Discord guild').status,recon.Decision.RESOLVED)
        db.session.expire_all()
        self.assertEqual(db.session.get(DiscordChannelRecovery,self.rid).recovery_status,'RESOLVED')
        self.assertEqual(db.session.get(Appointment,self.booking.id).status,'cancelled')
    def test_invalid_notes_do_not_change_state(self):
        self.assertEqual(recon.decide(self.rid,self.aid_admin,0,'RESOLVE','x').status,recon.Decision.INVALID)
        self.assertEqual(db.session.get(DiscordChannelRecovery,self.rid).recovery_status,'PENDING')
    def test_duplicate_resolution_is_stale(self):
        recon.decide(self.rid,self.aid_admin,0,'RESOLVE','Confirmed manually on Discord')
        self.assertEqual(recon.decide(self.rid,self.aid_admin,1,'RESOLVE','Confirmed manually on Discord').status,recon.Decision.STALE)
    def test_route_nonadmin_cannot_modify(self):
        self.login(self.aid_other)
        r=self.client.post(f'/api/admin/discord/recoveries/{self.rid}/resolve',json={'review_version':0,'notes':'Verified channel gone externally'})
        self.assertEqual(r.status_code,403)
    def test_route_admin_can_flag(self):
        self.login(self.aid_admin)
        r=self.client.post(f'/api/admin/discord/recoveries/{self.rid}/flag',json={'review_version':0,'notes':'Needs additional investigation'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['status'],'FLAGGED')
    def test_resolution_route_requires_external_attestation(self):
        self.login(self.aid_admin)
        r=self.client.post(f'/api/admin/discord/recoveries/{self.rid}/resolve',json={'review_version':0,'notes':'Verified channel status externally'})
        self.assertEqual(r.status_code,400)
        self.assertEqual(db.session.get(DiscordChannelRecovery,self.rid).recovery_status,'PENDING')
    def test_route_csrf_enforced(self):
        self.login(self.aid_admin)
        self.app.config['WTF_CSRF_ENABLED']=True
        try:
            r=self.client.post(f'/api/admin/discord/recoveries/{self.rid}/flag',json={'review_version':0,'notes':'Needs additional investigation'})
            self.assertEqual(r.status_code,400)
        finally:
            self.app.config['WTF_CSRF_ENABLED']=False
    def test_no_sensitive_data_in_listing(self):
        self.login(self.aid_admin)
        body=self.client.get('/api/admin/discord/recoveries').get_data(as_text=True)
        self.assertNotIn('attempt_token',body)
        self.assertNotIn('bot_token',body)
