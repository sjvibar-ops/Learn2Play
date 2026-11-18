"""Feature 3 recovery ledger tests (isolated SQLite, no live external calls)."""
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

import sqlalchemy as sa

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import Appointment, User, DiscordChannelRecovery
    from Backend import discord_lifecycle as lifecycle
    from Backend import discord_channel_orchestration as orch
    from Backend import discord_oauth as discord
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import Appointment, User, DiscordChannelRecovery
    import discord_lifecycle as lifecycle
    import discord_channel_orchestration as orch
    import discord_oauth as discord

SETTINGS = {'guild_id': '123456789012345678', 'bot_token': 'dummy-only'}
STUDENT = '123456789012345679'
PROVIDER = '123456789012345680'
CHANNEL = '123456789012345681'
TOKEN = 'a' * 64


class RecoveryLedgerTests(TestCase):
    def setUp(self):
        self.app = create_app({
            'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
            'WTF_CSRF_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(db.engine.url.database, ':memory:')
        db.create_all()
        self.student = User(display_name='Student', email='student@recover.test', password_hash='x', discord_user_id=STUDENT)
        self.provider = User(display_name='Provider', email='provider@recover.test', password_hash='x', discord_user_id=PROVIDER)
        db.session.add_all([self.student, self.provider])
        db.session.flush()
        self.appt = Appointment(student_id=self.student.id, provider_id=self.provider.id,
                                topic='Recovery', role='mentor', status='accepted',
                                start_time=datetime(2026, 12, 1))
        db.session.add(self.appt)
        db.session.commit()
        self.aid = self.appt.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def rows(self):
        db.session.expire_all()
        return db.session.execute(sa.select(DiscordChannelRecovery).order_by(DiscordChannelRecovery.id)).scalars().all()

    def test_ledger_schema_in_fresh_test_database(self):
        cols = {c['name'] for c in sa.inspect(db.engine).get_columns('discord_channel_recoveries')}
        self.assertTrue({'event_key', 'appointment_id', 'channel_id', 'recovery_reason', 'recovery_status'} <= cols)

    def test_begin_attempt_committed_and_idempotent(self):
        self.assertEqual(lifecycle.begin_attempt(self.aid, TOKEN).status, lifecycle.RecoveryStatus.RECORDED)
        self.assertEqual(lifecycle.begin_attempt(self.aid, TOKEN).status, lifecycle.RecoveryStatus.ALREADY_RECORDED)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].recovery_reason, 'ATTEMPT_STARTED')
        self.assertNotIn(TOKEN, self.rows()[0].event_key)

    def test_invalid_attempt_does_not_write(self):
        self.assertEqual(lifecycle.begin_attempt(self.aid, 'bad').status, lifecycle.RecoveryStatus.INVALID_INPUT)
        self.assertEqual(self.rows(), [])

    def test_uncertain_attempt_pending_and_not_retried(self):
        lifecycle.begin_attempt(self.aid, TOKEN)
        self.assertEqual(lifecycle.update_attempt(self.aid, TOKEN, 'UNCERTAIN_CREATION').status,
                         lifecycle.RecoveryStatus.RECORDED)
        self.assertEqual(self.rows()[0].recovery_status, 'PENDING')
        self.assertEqual(self.rows()[0].recovery_reason, 'UNCERTAIN_CREATION')

    def test_known_orphan_channel_persisted(self):
        lifecycle.begin_attempt(self.aid, TOKEN)
        lifecycle.update_attempt(self.aid, TOKEN, 'ORPHAN_CANDIDATE', channel_id=CHANNEL)
        self.assertEqual(self.rows()[0].channel_id, CHANNEL)
        self.assertEqual(self.rows()[0].recovery_status, 'PENDING')
        self.assertEqual(lifecycle.update_attempt(self.aid, TOKEN, 'RESOLVED').status,
                         lifecycle.RecoveryStatus.INVALID_INPUT)
        self.assertEqual(self.rows()[0].channel_id, CHANNEL)

    def test_success_ledger_resolved(self):
        lifecycle.begin_attempt(self.aid, TOKEN)
        lifecycle.update_attempt(self.aid, TOKEN, 'RESOLVED', channel_id=CHANNEL)
        self.assertEqual(self.rows()[0].recovery_status, 'RESOLVED')
        self.assertEqual(lifecycle.review_pending_recoveries(), [])

    def test_terminal_cleanup_in_booking_transaction(self):
        self.appt.status = 'completed'
        self.appt.discord_channel_id = CHANNEL
        self.appt.discord_channel_status = 'provisioned'
        self.assertTrue(lifecycle.queue_terminal_cleanup(self.appt, 'COMPLETED'))
        db.session.commit()
        self.assertEqual(self.rows()[0].recovery_reason, 'COMPLETED')
        self.assertEqual(self.rows()[0].channel_id, CHANNEL)
        self.assertEqual(self.rows()[0].recovery_status, 'PENDING')

    def test_terminal_queue_rolls_back_with_booking(self):
        self.appt.status = 'cancelled'
        self.appt.discord_channel_id = CHANNEL
        lifecycle.queue_terminal_cleanup(self.appt, 'CANCELLED')
        db.session.rollback()
        self.assertEqual(self.rows(), [])
        db.session.expire_all()
        self.assertEqual(db.session.get(Appointment, self.aid).status, 'accepted')

    def test_no_channel_no_terminal_record(self):
        self.appt.status = 'cancelled'
        self.assertFalse(lifecycle.queue_terminal_cleanup(self.appt, 'CANCELLED'))
        db.session.commit()
        self.assertEqual(self.rows(), [])

    def test_review_never_deletes_discord_channel(self):
        self.appt.status = 'cancelled'
        self.appt.discord_channel_id = CHANNEL
        lifecycle.queue_terminal_cleanup(self.appt, 'CANCELLED')
        db.session.commit()
        with patch.object(discord, 'delete_voice_channel', side_effect=AssertionError('Never DELETE')):
            pending = lifecycle.review_pending_recoveries()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]['requires_manual_review'])
        self.assertFalse(pending[0]['referenced_by_active_booking'])
        self.assertEqual(self.rows()[0].recovery_status, 'PENDING')

    def test_active_reference_protected_during_review(self):
        self.appt.discord_channel_id = CHANNEL
        lifecycle.begin_attempt(self.aid, TOKEN)
        lifecycle.update_attempt(self.aid, TOKEN, 'ORPHAN_CANDIDATE', channel_id=CHANNEL)
        self.assertTrue(lifecycle.review_pending_recoveries()[0]['referenced_by_active_booking'])

    def test_orchestration_created_record_resolved(self):
        response = {'ok': True, 'status': discord.VOICE_CHANNEL_CREATED, 'channel_id': CHANNEL}
        with patch.object(discord, 'create_private_voice_channel', return_value=response):
            result = orch.provision_voice_channel(self.aid, settings=SETTINGS)
        self.assertEqual(result.status, orch.OrchestrationStatus.CREATED)
        self.assertEqual(self.rows()[0].recovery_status, 'RESOLVED')
        self.assertEqual(self.rows()[0].channel_id, CHANNEL)

    def test_orchestration_unknown_has_durable_record(self):
        with patch.object(discord, 'create_private_voice_channel', side_effect=TimeoutError):
            result = orch.provision_voice_channel(self.aid, settings=SETTINGS)
        self.assertEqual(result.status, orch.OrchestrationStatus.UNCERTAIN)
        self.assertEqual(self.rows()[0].recovery_reason, 'UNCERTAIN_CREATION')
        self.assertEqual(self.rows()[0].recovery_status, 'PENDING')

    def test_orchestration_does_not_call_discord_if_ledger_fails(self):
        with patch.object(lifecycle, 'begin_attempt', return_value=lifecycle.RecoveryResult(lifecycle.RecoveryStatus.DATABASE_ERROR)):
            with patch.object(discord, 'create_private_voice_channel') as create:
                result = orch.provision_voice_channel(self.aid, settings=SETTINGS)
        self.assertEqual(result.status, orch.OrchestrationStatus.RECONCILIATION_REQUIRED)
        create.assert_not_called()


    def test_booking_cancel_route_enqueues_recovery(self):
        self.appt.discord_channel_id = CHANNEL
        self.appt.discord_channel_status = 'provisioned'
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(self.student.id)
            session['_fresh'] = True
        response = client.post(f'/api/bookings/{self.aid}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows()[0].recovery_reason, 'CANCELLED')
        self.assertEqual(db.session.get(Appointment, self.aid).status, 'cancelled')

    def test_booking_cancel_without_channel_does_not_enqueue(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(self.student.id)
            session['_fresh'] = True
        response = client.post(f'/api/bookings/{self.aid}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows(), [])

    def test_booking_cancel_queue_failure_rolls_back_status(self):
        self.appt.discord_channel_id = CHANNEL
        self.appt.discord_channel_status = 'provisioned'
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(self.student.id)
            session['_fresh'] = True
        with patch.object(lifecycle, 'queue_terminal_cleanup', side_effect=RuntimeError('fake-db-error')):
            response = client.post(f'/api/bookings/{self.aid}/cancel')
        self.assertEqual(response.status_code, 503)
        db.session.expire_all()
        self.assertEqual(db.session.get(Appointment, self.aid).status, 'accepted')
        self.assertEqual(self.rows(), [])

    def test_invalid_review_limit(self):
        with self.assertRaises(ValueError):
            lifecycle.review_pending_recoveries(0)
        with self.assertRaises(ValueError):
            lifecycle.review_pending_recoveries(501)
