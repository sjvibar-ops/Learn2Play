"""Fail-closed, DB-only retirement preparation tests; no Discord API."""
from datetime import datetime
from unittest import TestCase

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import User, Appointment, DiscordChannelRecovery
    from Backend.discord_cleanup_fencing import claim_for_review, Claim
    from Backend.discord_channel_retirement import prepare_retirement, Retirement
    from Backend.discord_provisioning import claim_provisioning, ClaimStatus
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import User, Appointment, DiscordChannelRecovery
    from discord_cleanup_fencing import claim_for_review, Claim
    from discord_channel_retirement import prepare_retirement, Retirement
    from discord_provisioning import claim_provisioning, ClaimStatus


class RetirementPreparationTests(TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
                               'WTF_CSRF_ENABLED': False, 'DISCORD_SESSIONS_ENABLED': False})
        self.ctx = self.app.app_context()
        self.ctx.push()
        assert db.engine.url.database == ':memory:'
        db.create_all()
        student = User(display_name='Student', email='retire-student@test.invalid', password_hash='x')
        teacher = User(display_name='Teacher', email='retire-teacher@test.invalid', password_hash='x')
        db.session.add_all((student, teacher))
        db.session.flush()
        self.appointment = Appointment(student_id=student.id, provider_id=teacher.id,
            topic='Retirement', role='mentor', start_time=datetime(2026, 12, 1),
            status='completed', discord_channel_id='123456789012345678',
            discord_channel_status='provisioned')
        db.session.add(self.appointment)
        db.session.flush()
        self.recovery = DiscordChannelRecovery(event_key='terminal:retire:1',
            appointment_id=self.appointment.id, channel_id=self.appointment.discord_channel_id,
            recovery_reason='COMPLETED', recovery_status='PENDING')
        db.session.add(self.recovery)
        db.session.commit()
        self.aid = self.appointment.id
        self.rid = self.recovery.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _claim(self):
        claim = claim_for_review(self.rid, 'test-worker')
        self.assertEqual(claim.status, Claim.CLAIMED)
        return claim.token

    def test_success_prepares_both_rows_but_never_authorizes_delete(self):
        token = self._claim()
        result = prepare_retirement(self.rid, token)
        self.assertEqual(result.status, Retirement.PREPARED)
        self.assertFalse(result.deletion_permitted)
        db.session.expire_all()
        self.assertEqual(db.session.get(Appointment, self.aid).discord_channel_status, 'deprovisioning')
        self.assertEqual(db.session.get(DiscordChannelRecovery, self.rid).cleanup_state, 'RETIREMENT_PREPARED')

    def test_repeat_prepare_is_rejected_not_retried(self):
        token = self._claim()
        self.assertEqual(prepare_retirement(self.rid, token).status, Retirement.PREPARED)
        self.assertEqual(prepare_retirement(self.rid, token).status, Retirement.REJECTED)

    def test_invalid_and_stale_tokens_rejected(self):
        token = self._claim()
        self.assertEqual(prepare_retirement(self.rid, 'wrong').status, Retirement.INVALID)
        self.assertEqual(prepare_retirement(self.rid, '0' * 64).status, Retirement.REJECTED)
        self.assertEqual(prepare_retirement(True, token).status, Retirement.INVALID)
        self.assertEqual(db.session.get(Appointment, self.aid).discord_channel_status, 'provisioned')

    def test_active_booking_never_prepares(self):
        db.session.get(Appointment, self.aid).status = 'accepted'
        db.session.commit()
        self.assertEqual(claim_for_review(self.rid, 'worker').status, Claim.REJECTED)
        # Even if the claim was previously obtained, the terminal guard holds.
        db.session.get(Appointment, self.aid).status = 'completed'
        db.session.commit()
        token = self._claim()
        db.session.get(Appointment, self.aid).status = 'scheduled'
        db.session.commit()
        self.assertEqual(prepare_retirement(self.rid, token).status, Retirement.REJECTED)
        self.assertEqual(db.session.get(Appointment, self.aid).discord_channel_status, 'provisioned')

    def test_recovery_channel_mismatch_rejected(self):
        token = self._claim()
        db.session.get(DiscordChannelRecovery, self.rid).channel_id = '999999999999999999'
        db.session.commit()
        self.assertEqual(prepare_retirement(self.rid, token).status, Retirement.REJECTED)

    def test_orphan_reason_never_prepares_linked_booking(self):
        token = self._claim()
        db.session.get(DiscordChannelRecovery, self.rid).recovery_reason = 'ORPHAN_CANDIDATE'
        db.session.commit()
        self.assertEqual(prepare_retirement(self.rid, token).status, Retirement.REJECTED)

    def test_attempt_cannot_be_reclaimed_after_retirement(self):
        token = self._claim()
        self.assertEqual(prepare_retirement(self.rid, token).status, Retirement.PREPARED)
        self.assertEqual(claim_provisioning(self.aid).status, ClaimStatus.INELIGIBLE_BOOKING)

    def test_review_claim_is_not_retirement_authorization(self):
        token = self._claim()
        self.assertFalse(hasattr(__import__(prepare_retirement.__module__, fromlist=['delete_voice_channel']),
                                 'delete_voice_channel'))
        self.assertEqual(db.session.get(Appointment, self.aid).discord_channel_status, 'provisioned')
        self.assertEqual(db.session.get(DiscordChannelRecovery, self.rid).cleanup_state, 'REVIEW_CLAIMED')
