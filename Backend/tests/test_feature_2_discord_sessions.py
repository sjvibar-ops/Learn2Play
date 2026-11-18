"""Feature 2 Discord booking endpoint; hermetic tests (no live Discord calls)."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch, Mock

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import User, Appointment
    from Backend import routes, discord_channel_orchestration as orch, discord_oauth as discord
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import User, Appointment
    import routes
    import discord_channel_orchestration as orch
    import discord_oauth as discord

STUDENT_ID = '111222333444555666'
PROVIDER_ID = '222333444555666777'
GUILD_ID = '333444555666777888'
CHANNEL_ID = '444555666777888999'


class DiscordBookingSessionEndpointTests(TestCase):
    def setUp(self):
        self.app = create_app({
            'TESTING': True, 'WTF_CSRF_ENABLED': False,
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
            'DISCORD_SESSIONS_ENABLED': True,
            'DISCORD_GUILD_ID': GUILD_ID,
            'DISCORD_BOT_TOKEN': 'disposable-test-token',
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(db.engine.url.database, ':memory:')
        db.create_all()
        self.student = User(display_name='Student', email='discord-student@example.test', password_hash='test', roles_json='["student"]', discord_user_id=STUDENT_ID)
        self.provider = User(display_name='Provider', email='discord-provider@example.test', password_hash='test', roles_json='["mentor"]', discord_user_id=PROVIDER_ID)
        self.other = User(display_name='Other', email='discord-other@example.test', password_hash='test', roles_json='["student"]')
        db.session.add_all([self.student, self.provider, self.other])
        db.session.flush()
        self.student_id = self.student.id
        self.provider_id = self.provider.id
        self.other_id = self.other.id
        self.appointment = Appointment(
            student_id=self.student_id, provider_id=self.provider_id, role='mentor',
            topic='Practice', start_time=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            duration_minutes=60, status='accepted', meeting_room='l2-discord-feature-test',
        )
        db.session.add(self.appointment)
        db.session.commit()
        self.booking_id = self.appointment.id
        self.client = self.app.test_client()
        self.membership = patch.object(routes.discord_oauth, 'check_guild_membership', return_value={'status': 'VERIFIED_MEMBER', 'is_member': True})
        self.membership_mock = self.membership.start()
        self.prov = patch.object(routes, 'provider_approved_for_role', return_value=True)
        self.prov.start()
        self.orchestrator = patch.object(routes.discord_channel_orchestration, 'provision_voice_channel',
            return_value=orch.OrchestrationResult(orch.OrchestrationStatus.CREATED, channel_id=CHANNEL_ID))
        self.orch_mock = self.orchestrator.start()

    def tearDown(self):
        self.orchestrator.stop()
        self.prov.stop()
        self.membership.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login(self, user_id=None):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user_id if user_id is not None else self.student_id)
            session['_fresh'] = True

    def call(self):
        return self.client.post(f'/api/sessions/{self.booking_id}/discord-channel')

    def assert_no_create(self):
        self.orch_mock.assert_not_called()

    def test_disabled_by_default_gate(self):
        self.login()
        self.app.config['DISCORD_SESSIONS_ENABLED'] = False
        self.assertEqual(self.call().status_code, 503)
        self.assert_no_create()

    def test_csrf_required_when_protection_enabled(self):
        self.login()
        self.app.config['WTF_CSRF_ENABLED'] = True
        try:
            response = self.call()
            self.assertEqual(response.status_code, 400)
            self.assert_no_create()
        finally:
            self.app.config['WTF_CSRF_ENABLED'] = False

    def test_anonymous_rejected(self):
        self.assertIn(self.call().status_code, (302, 401))
        self.assert_no_create()

    def test_other_user_cannot_probe_booking(self):
        self.login(self.other_id)
        self.assertEqual(self.call().status_code, 404)
        self.assert_no_create()

    def test_provider_is_authorized(self):
        self.login(self.provider_id)
        self.assertEqual(self.call().status_code, 201)

    def test_student_happy_path_returns_fixed_discord_link(self):
        self.login()
        response = self.call()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['url'], f'https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.membership_mock.call_count, 2)
        self.orch_mock.assert_called_once()

    def test_reused_channel_returns_200(self):
        self.login()
        self.orch_mock.return_value = orch.OrchestrationResult(orch.OrchestrationStatus.ALREADY_PROVISIONED, channel_id=CHANNEL_ID)
        self.assertEqual(self.call().status_code, 200)

    def test_noneligible_booking_states(self):
        self.login()
        for state in ('pending', 'rejected', 'cancelled', 'completed'):
            self.appointment.status = state
            db.session.commit()
            self.assertEqual(self.call().status_code, 403)
        self.assert_no_create()

    def test_not_yet_in_session_window(self):
        self.login()
        self.appointment.start_time += timedelta(days=1)
        db.session.commit()
        self.assertEqual(self.call().status_code, 403)
        self.assert_no_create()

    def test_expired_booking(self):
        self.login()
        self.appointment.start_time -= timedelta(days=2)
        db.session.commit()
        self.assertEqual(self.call().status_code, 403)
        self.assert_no_create()

    def test_provider_not_approved(self):
        self.login()
        with patch.object(routes, 'provider_approved_for_role', return_value=False):
            self.assertEqual(self.call().status_code, 403)
        self.assert_no_create()

    def test_missing_discord_link(self):
        self.login()
        self.student.discord_user_id = None
        db.session.commit()
        self.assertEqual(self.call().status_code, 409)
        self.assert_no_create()

    def test_missing_provider_link(self):
        self.login()
        self.provider.discord_user_id = None
        db.session.commit()
        self.assertEqual(self.call().status_code, 409)
        self.assert_no_create()

    def test_discord_member_not_in_guild(self):
        self.login()
        self.membership_mock.return_value = {'status': 'NOT_IN_GUILD', 'is_member': False}
        self.assertEqual(self.call().status_code, 403)
        self.assert_no_create()

    def test_membership_unavailable_fails_closed(self):
        self.login()
        self.membership_mock.return_value = {'status': 'CHECK_UNAVAILABLE', 'is_member': None}
        self.assertEqual(self.call().status_code, 503)
        self.assert_no_create()

    def test_bot_configuration_missing(self):
        self.login()
        self.app.config['DISCORD_BOT_TOKEN'] = ''
        self.assertEqual(self.call().status_code, 503)
        self.assert_no_create()

    def test_concurrent_claim_in_progress(self):
        self.login()
        self.orch_mock.return_value = orch.OrchestrationResult(orch.OrchestrationStatus.CLAIM_NOT_ACQUIRED)
        self.assertEqual(self.call().status_code, 409)

    def test_uncertain_outcome_reports_safe_error(self):
        self.login()
        self.orch_mock.return_value = orch.OrchestrationResult(orch.OrchestrationStatus.RECONCILIATION_REQUIRED,
            orphan_candidate_channel_id=CHANNEL_ID)
        response = self.call()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(CHANNEL_ID, response.get_data(as_text=True))
        self.assertNotIn('disposable-test-token', response.get_data(as_text=True))

    def test_rate_limit(self):
        self.login()
        self.orch_mock.return_value = orch.OrchestrationResult(orch.OrchestrationStatus.DISCORD_RATE_LIMITED, retry_after=15.0)
        response = self.call()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get('Retry-After'), '15')

    def test_database_error(self):
        self.login()
        self.orch_mock.return_value = orch.OrchestrationResult(orch.OrchestrationStatus.DATABASE_ERROR)
        self.assertEqual(self.call().status_code, 503)

    def test_membership_checked_on_each_request(self):
        self.login()
        self.call()
        self.call()
        self.assertEqual(self.membership_mock.call_count, 4)


if __name__ == '__main__':
    import unittest
    unittest.main()
