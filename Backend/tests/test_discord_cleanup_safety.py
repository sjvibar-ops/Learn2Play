"""Fail-closed cleanup diagnostic tests; no external APIs or deletion."""
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import Appointment, User, DiscordChannelRecovery
    from Backend.discord_cleanup_safety import assess_recovery, Safety
    from Backend import discord_oauth
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import Appointment, User, DiscordChannelRecovery
    from discord_cleanup_safety import assess_recovery, Safety
    import discord_oauth

CHANNEL = '123456789012345678'


class CleanupSafetyTests(TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
                               'WTF_CSRF_ENABLED': False})
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(db.engine.url.database, ':memory:')
        db.create_all()
        student = User(display_name='Student', email='cleanup-student@test.invalid', password_hash='x')
        provider = User(display_name='Provider', email='cleanup-provider@test.invalid', password_hash='x')
        db.session.add_all((student, provider))
        db.session.flush()
        booking = Appointment(student_id=student.id, provider_id=provider.id,
                              topic='Cleanup', role='mentor', start_time=datetime(2026, 12, 1),
                              status='cancelled', discord_channel_id=CHANNEL,
                              discord_channel_status='provisioned')
        db.session.add(booking)
        db.session.flush()
        recovery = DiscordChannelRecovery(appointment_id=booking.id,
            event_key='terminal:cleanup:1', channel_id=CHANNEL,
            recovery_reason='CANCELLED', recovery_status='PENDING')
        db.session.add(recovery)
        db.session.commit()
        self.booking_id = booking.id
        self.recovery_id = recovery.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def check(self, expected):
        with patch.object(discord_oauth, 'delete_voice_channel',
                          side_effect=AssertionError('Discord called')):
            result = assess_recovery(self.recovery_id)
        self.assertEqual(result.status, expected)
        self.assertFalse(result.deletion_permitted)
        return result

    def test_terminal_booking_is_candidate_not_delete_authorization(self):
        self.check(Safety.CLEANUP_CANDIDATE)

    def test_active_reference_blocks(self):
        db.session.get(Appointment, self.booking_id).status = 'accepted'
        db.session.commit()
        self.check(Safety.ACTIVE_REFERENCE)

    def test_provisioning_reference_is_ambiguous(self):
        db.session.get(Appointment, self.booking_id).discord_channel_status = 'provisioning'
        db.session.commit()
        self.check(Safety.AMBIGUOUS_OWNERSHIP)

    def test_uncertain_creation_never_eligible(self):
        row = db.session.get(DiscordChannelRecovery, self.recovery_id)
        row.recovery_reason = 'UNCERTAIN_CREATION'
        row.channel_id = None
        db.session.commit()
        self.check(Safety.AMBIGUOUS_OWNERSHIP)

    def test_conflicting_unresolved_ledger_blocks(self):
        db.session.add(DiscordChannelRecovery(appointment_id=self.booking_id,
            event_key='other:cleanup:1', channel_id=CHANNEL,
            recovery_reason='ORPHAN_CANDIDATE', recovery_status='PENDING'))
        db.session.commit()
        self.check(Safety.AMBIGUOUS_OWNERSHIP)

    def test_resolved_is_not_cleanup_permission(self):
        db.session.get(DiscordChannelRecovery, self.recovery_id).recovery_status = 'RESOLVED'
        db.session.commit()
        self.check(Safety.AMBIGUOUS_OWNERSHIP)

    def test_unknown_and_invalid_ids_fail_closed(self):
        self.assertEqual(assess_recovery(99999).status, Safety.UNKNOWN)
        with self.assertRaises(ValueError):
            assess_recovery(0)
        with self.assertRaises(ValueError):
            assess_recovery(True)

    def test_assessment_is_read_only(self):
        before = db.session.get(DiscordChannelRecovery, self.recovery_id).review_version
        self.check(Safety.CLEANUP_CANDIDATE)
        db.session.expire_all()
        self.assertEqual(db.session.get(DiscordChannelRecovery, self.recovery_id).review_version, before)
        self.assertEqual(db.session.get(Appointment, self.booking_id).discord_channel_status, 'provisioned')
