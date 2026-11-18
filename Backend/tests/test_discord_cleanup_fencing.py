"""Safety tests: claims never authorize delete, no external Discord access."""
from datetime import datetime
from unittest import TestCase
try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import User, Appointment, DiscordChannelRecovery
    from Backend.discord_cleanup_fencing import claim_for_review, mark_uncertain, Claim
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import User, Appointment, DiscordChannelRecovery
    from discord_cleanup_fencing import claim_for_review, mark_uncertain, Claim


class CleanupFencingTests(TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:', 'WTF_CSRF_ENABLED': False})
        self.ctx = self.app.app_context(); self.ctx.push()
        assert db.engine.url.database == ':memory:'
        db.create_all()
        a = User(display_name='A', email='fence-a@test.invalid', password_hash='x')
        b = User(display_name='B', email='fence-b@test.invalid', password_hash='x')
        db.session.add_all((a, b)); db.session.flush()
        booking = Appointment(student_id=a.id, provider_id=b.id, topic='Fence', role='mentor',
             start_time=datetime(2026, 12, 1), status='cancelled',
             discord_channel_id='123456789012345678', discord_channel_status='provisioned')
        db.session.add(booking); db.session.flush()
        recovery = DiscordChannelRecovery(event_key='terminal:fence:1', appointment_id=booking.id,
             channel_id=booking.discord_channel_id, recovery_reason='CANCELLED', recovery_status='PENDING')
        db.session.add(recovery); db.session.commit()
        self.booking_id=booking.id; self.rid=recovery.id

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def test_only_first_claim_wins(self):
        first=claim_for_review(self.rid, 'worker-1')
        self.assertEqual(first.status, Claim.CLAIMED)
        self.assertFalse(first.deletion_permitted)
        self.assertEqual(len(first.token), 64)
        self.assertEqual(claim_for_review(self.rid, 'worker-2').status, Claim.REJECTED)

    def test_wrong_token_cannot_change_state(self):
        token=claim_for_review(self.rid, 'worker-1').token
        self.assertFalse(mark_uncertain(self.rid, '0'*64))
        self.assertTrue(mark_uncertain(self.rid, token))
        self.assertFalse(mark_uncertain(self.rid, token))

    def test_invalid_values_fail_closed(self):
        self.assertEqual(claim_for_review(True,'worker').status, Claim.INVALID)
        self.assertEqual(claim_for_review(self.rid,'').status, Claim.INVALID)
        self.assertEqual(claim_for_review(self.rid,'bad worker').status, Claim.INVALID)
        self.assertFalse(mark_uncertain(self.rid, 'bad'))

    def test_active_reference_blocks(self):
        db.session.get(Appointment,self.booking_id).status='accepted'; db.session.commit()
        self.assertEqual(claim_for_review(self.rid,'w1').status, Claim.REJECTED)

    def test_uncertain_creation_blocks(self):
        row=db.session.get(DiscordChannelRecovery,self.rid)
        row.recovery_reason='UNCERTAIN_CREATION'; db.session.commit()
        self.assertEqual(claim_for_review(self.rid,'w1').status, Claim.REJECTED)

    def test_conflicting_unresolved_record_blocks(self):
        row=db.session.get(DiscordChannelRecovery,self.rid)
        db.session.add(DiscordChannelRecovery(event_key='another:fence:2', appointment_id=self.booking_id,
             channel_id=row.channel_id, recovery_reason='ORPHAN_CANDIDATE', recovery_status='PENDING'))
        db.session.commit()
        self.assertEqual(claim_for_review(self.rid,'w1').status, Claim.REJECTED)

    def test_no_channel_blocks(self):
        db.session.get(DiscordChannelRecovery,self.rid).channel_id=None; db.session.commit()
        self.assertEqual(claim_for_review(self.rid,'w1').status, Claim.REJECTED)

    def test_claim_does_not_change_appointment_or_authorize_delete(self):
        result=claim_for_review(self.rid, 'w1')
        self.assertEqual(result.status, Claim.CLAIMED)
        db.session.expire_all()
        ap=db.session.get(Appointment,self.booking_id)
        self.assertEqual(ap.discord_channel_status,'provisioned')
        self.assertEqual(ap.status,'cancelled')
        self.assertFalse(result.deletion_permitted)
