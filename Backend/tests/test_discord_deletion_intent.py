"""Deletion-intent claims are database-only and NEVER authorize network deletion."""
from datetime import datetime
from unittest import TestCase

from Backend import create_app
from Backend.extensions import db
from Backend.models import Appointment, User, DiscordChannelRecovery
from Backend.discord_cleanup_fencing import claim_for_review, Claim
from Backend.discord_channel_retirement import prepare_retirement, Retirement
from Backend.discord_deletion_intent import claim_deletion_intent, DeletionClaim


class DeletionIntentTests(TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
                               'WTF_CSRF_ENABLED': False, 'DISCORD_SESSIONS_ENABLED': False})
        self.ctx = self.app.app_context()
        self.ctx.push()
        assert db.engine.url.database == ':memory:'
        db.create_all()
        student = User(display_name='Student', email='deletion-student@test.invalid', password_hash='x')
        provider = User(display_name='Provider', email='deletion-provider@test.invalid', password_hash='x')
        db.session.add_all((student, provider))
        db.session.flush()
        appt = Appointment(student_id=student.id, provider_id=provider.id,
                           topic='Test', role='mentor', start_time=datetime(2026,12,1),
                           status='completed', discord_channel_id='123456789012345678',
                           discord_channel_status='provisioned')
        db.session.add(appt)
        db.session.flush()
        rec = DiscordChannelRecovery(event_key='terminal:del:1', appointment_id=appt.id,
                                     channel_id=appt.discord_channel_id,
                                     recovery_reason='COMPLETED', recovery_status='PENDING')
        db.session.add(rec)
        db.session.commit()
        self.aid, self.rid = appt.id, rec.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _prepare(self):
        c = claim_for_review(self.rid, 'deletion-test')
        self.assertEqual(c.status, Claim.CLAIMED)
        self.assertEqual(prepare_retirement(self.rid, c.token).status, Retirement.PREPARED)
        return c.token

    def test_claim_requires_retirement_preparation(self):
        self.assertEqual(claim_deletion_intent(self.rid, '0'*64).status, DeletionClaim.REJECTED)

    def test_one_shot_claim_never_grants_delete(self):
        tok = self._prepare()
        res = claim_deletion_intent(self.rid, tok)
        self.assertEqual(res.status, DeletionClaim.CLAIMED)
        self.assertFalse(res.deletion_permitted)
        self.assertEqual(len(res.token), 64)
        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.cleanup_state, 'DELETION_INTENT_CLAIMED')
        self.assertEqual(rec.cleanup_fencing_token, res.token)
        self.assertEqual(db.session.get(Appointment,self.aid).discord_channel_status, 'deprovisioning')

    def test_second_claim_is_rejected_and_original_token_is_preserved(self):
        tok = self._prepare()
        first = claim_deletion_intent(self.rid, tok)
        self.assertEqual(first.status, DeletionClaim.CLAIMED)
        self.assertEqual(claim_deletion_intent(self.rid, tok).status, DeletionClaim.REJECTED)
        self.assertEqual(claim_deletion_intent(self.rid, first.token).status, DeletionClaim.REJECTED)
        db.session.expire_all()
        self.assertEqual(db.session.get(DiscordChannelRecovery,self.rid).cleanup_fencing_token, first.token)

    def test_stale_review_token_rejected(self):
        self._prepare()
        self.assertEqual(claim_deletion_intent(self.rid,'f'*64).status, DeletionClaim.REJECTED)

    def test_active_appointment_cannot_claim(self):
        tok = self._prepare()
        appt = db.session.get(Appointment,self.aid)
        appt.status = 'scheduled'
        db.session.commit()
        self.assertEqual(claim_deletion_intent(self.rid,tok).status,DeletionClaim.REJECTED)

    def test_conflicting_recovery_even_if_resolved_blocks(self):
        tok = self._prepare()
        db.session.add(DiscordChannelRecovery(event_key='other:del:2',appointment_id=self.aid,
            channel_id='123456789012345678',recovery_reason='RESOLVED',recovery_status='RESOLVED'))
        db.session.commit()
        self.assertEqual(claim_deletion_intent(self.rid,tok).status,DeletionClaim.REJECTED)

    def test_mismatched_channel_blocks(self):
        tok = self._prepare()
        db.session.get(DiscordChannelRecovery,self.rid).channel_id='999999999999999999'
        db.session.commit()
        self.assertEqual(claim_deletion_intent(self.rid,tok).status,DeletionClaim.REJECTED)

    def test_invalid_input_and_no_http_import(self):
        from Backend import discord_deletion_intent as module
        self.assertEqual(claim_deletion_intent(True,'0'*64).status,DeletionClaim.INVALID)
        self.assertEqual(claim_deletion_intent(self.rid,'bad').status,DeletionClaim.INVALID)
        self.assertFalse(hasattr(module,'delete_voice_channel'))
        self.assertNotIn('requests',module.__dict__)
