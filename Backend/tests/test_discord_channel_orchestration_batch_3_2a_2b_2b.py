"""Batch 3.2A-2B-2B orchestration tests: ONLY mocked Discord, disposable SQLite."""
import unittest
from datetime import datetime
from unittest.mock import patch

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import User, Appointment
    from Backend import discord_channel_orchestration as orch
    from Backend import discord_oauth as discord
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import User, Appointment
    import discord_channel_orchestration as orch
    import discord_oauth as discord


GUILD = "123456789012345678"
STUDENT = "123456789012345679"
PROVIDER = "123456789012345680"
CHANNEL = "123456789012345681"
SETTINGS = {"guild_id": GUILD, "bot_token": "fake-test-token"}


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        provider = User(display_name="Provider", email="provider@orch.test", password_hash="x",
                        discord_user_id=PROVIDER)
        student = User(display_name="Student", email="student@orch.test", password_hash="x",
                       discord_user_id=STUDENT)
        db.session.add_all([provider, student])
        db.session.commit()
        self.provider_id, self.student_id = provider.id, student.id
        appt = Appointment(provider_id=self.provider_id, student_id=self.student_id,
                           topic="Test", role="mentor", start_time=datetime(2026, 12, 1), status="accepted")
        db.session.add(appt)
        db.session.commit()
        self.aid = appt.id
        self.created = {"ok": True, "status": discord.VOICE_CHANNEL_CREATED,
                        "channel_id": CHANNEL, "outcome_unknown": False}
        self.no_delete = patch.object(discord, "delete_voice_channel", side_effect=AssertionError("DELETE forbidden"))
        self.no_delete.start()

    def tearDown(self):
        self.no_delete.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def state(self):
        db.session.expire_all()
        return db.session.get(Appointment, self.aid)

    def run_with(self, result):
        with patch.object(discord, "create_private_voice_channel", return_value=result) as create:
            got = orch.provision_voice_channel(self.aid, settings=SETTINGS)
        return got, create

    def test_happy_path_and_private_arguments(self):
        got, create = self.run_with(self.created)
        self.assertEqual(got.status, orch.OrchestrationStatus.CREATED)
        self.assertEqual(got.channel_id, CHANNEL)
        create.assert_called_once_with(SETTINGS, f"session-{self.aid}", STUDENT, PROVIDER)
        self.assertEqual(self.state().discord_channel_status, "provisioned")
        self.assertIsNone(self.state().discord_channel_attempt_token)

    def test_idempotent_no_second_create(self):
        self.run_with(self.created)
        with patch.object(discord, "create_private_voice_channel") as create:
            got = orch.provision_voice_channel(self.aid, settings=SETTINGS)
        self.assertEqual(got.status, orch.OrchestrationStatus.ALREADY_PROVISIONED)
        self.assertEqual(got.channel_id, CHANNEL)
        create.assert_not_called()

    def test_missing_student_link(self):
        db.session.get(User, self.student_id).discord_user_id = None
        db.session.commit()
        with patch.object(discord, "create_private_voice_channel") as create:
            got = orch.provision_voice_channel(self.aid, settings=SETTINGS)
        self.assertEqual(got.status, orch.OrchestrationStatus.CONFIGURATION_ERROR)
        create.assert_not_called()
        self.assertEqual(self.state().discord_channel_status, "unprovisioned")

    def test_missing_provider_link(self):
        db.session.get(User, self.provider_id).discord_user_id = None
        db.session.commit()
        got, create = self.run_with(self.created)
        self.assertEqual(got.status, orch.OrchestrationStatus.CONFIGURATION_ERROR)
        create.assert_not_called()

    def test_identical_discord_ids_rejected(self):
        # The User model has a unique Discord identity constraint; instead mock
        # the preflight data to verify the orchestrator rejects identical IDs.
        with patch.object(orch, "_preflight", return_value=((STUDENT, STUDENT), None)):
            got, create = self.run_with(self.created)
        self.assertEqual(got.status, orch.OrchestrationStatus.CONFIGURATION_ERROR)
        create.assert_not_called()

    def test_bot_config_rejected(self):
        got = orch.provision_voice_channel(self.aid, settings={})
        self.assertEqual(got.status, orch.OrchestrationStatus.CONFIGURATION_ERROR)
        self.assertEqual(self.state().discord_channel_status, "unprovisioned")

    def test_pending_booking_rejected(self):
        self.state().status = "pending"
        db.session.commit()
        got, create = self.run_with(self.created)
        self.assertEqual(got.status, orch.OrchestrationStatus.PREFLIGHT_REJECTED)
        create.assert_not_called()

    def test_in_progress_claim_rejected(self):
        self.state().discord_channel_status = "provisioning"
        db.session.commit()
        got, create = self.run_with(self.created)
        self.assertEqual(got.status, orch.OrchestrationStatus.CLAIM_NOT_ACQUIRED)
        create.assert_not_called()

    def test_deterministic_failure(self):
        got, _ = self.run_with({"ok": False, "status": discord.VOICE_BOT_FORBIDDEN,
                                "outcome_unknown": False})
        self.assertEqual(got.status, orch.OrchestrationStatus.DISCORD_REJECTED)
        self.assertEqual(self.state().discord_channel_status, "failed")
        self.assertIsNone(self.state().discord_channel_attempt_token)

    def test_rate_limit_no_auto_retry(self):
        got, create = self.run_with({"ok": False, "status": discord.VOICE_RATE_LIMITED,
                                     "outcome_unknown": False, "retry_after": 9})
        self.assertEqual(got.status, orch.OrchestrationStatus.DISCORD_RATE_LIMITED)
        self.assertEqual(got.retry_after, 9)
        create.assert_called_once()

    def test_timeout_unknown_no_delete(self):
        got, create = self.run_with({"ok": False, "status": discord.VOICE_OUTCOME_UNKNOWN,
                                     "outcome_unknown": True})
        self.assertEqual(got.status, orch.OrchestrationStatus.UNCERTAIN)
        self.assertEqual(self.state().discord_channel_status, "uncertain")
        create.assert_called_once()

    def test_5xx_unknown(self):
        got, _ = self.run_with({"ok": False, "status": discord.VOICE_UNAVAILABLE,
                                "outcome_unknown": True})
        self.assertEqual(got.status, orch.OrchestrationStatus.UNCERTAIN)

    def test_exception_after_send_treated_unknown(self):
        with patch.object(discord, "create_private_voice_channel", side_effect=RuntimeError("secret")):
            got = orch.provision_voice_channel(self.aid, settings=SETTINGS)
        self.assertEqual(got.status, orch.OrchestrationStatus.UNCERTAIN)
        self.assertNotIn("secret", repr(got))

    def test_malformed_success_is_unknown(self):
        got, _ = self.run_with({"ok": True, "status": discord.VOICE_CHANNEL_CREATED,
                                "channel_id": "not-an-id"})
        self.assertEqual(got.status, orch.OrchestrationStatus.UNCERTAIN)

    def test_cancel_midflight_reconciliation_no_delete(self):
        def cancel(*args):
            db.session.get(Appointment, self.aid).status = "cancelled"
            db.session.commit()
            return self.created
        with patch.object(discord, "create_private_voice_channel", side_effect=cancel):
            got = orch.provision_voice_channel(self.aid, settings=SETTINGS)
        self.assertEqual(got.status, orch.OrchestrationStatus.RECONCILIATION_REQUIRED)
        self.assertEqual(got.orphan_candidate_channel_id, CHANNEL)
        self.assertEqual(self.state().status, "cancelled")
        self.assertIsNone(self.state().discord_channel_id)

    def test_finalization_failure_requires_reconciliation(self):
        with patch.object(orch.domain, "finalize_provisioning_success",
                          return_value=orch.domain.FinalizeResult(orch.domain.FinalizeStatus.DATABASE_ERROR)):
            got, _ = self.run_with(self.created)
        self.assertEqual(got.status, orch.OrchestrationStatus.RECONCILIATION_REQUIRED)
        self.assertEqual(got.orphan_candidate_channel_id, CHANNEL)

    def test_failure_finalization_failure_requires_reconciliation(self):
        with patch.object(orch.domain, "finalize_provisioning_failure",
                          return_value=orch.domain.FinalizeResult(orch.domain.FinalizeStatus.DATABASE_ERROR)):
            got, _ = self.run_with({"ok": False, "status": discord.VOICE_UNAVAILABLE,
                                    "outcome_unknown": True})
        self.assertEqual(got.status, orch.OrchestrationStatus.RECONCILIATION_REQUIRED)

    def test_invalid_appointment_id(self):
        self.assertEqual(orch.provision_voice_channel("1", settings=SETTINGS).status,
                         orch.OrchestrationStatus.PREFLIGHT_REJECTED)

    def test_no_automatic_compensation(self):
        with patch.object(orch.domain, "finalize_provisioning_success",
                          return_value=orch.domain.FinalizeResult(orch.domain.FinalizeStatus.FENCING_REJECTED)):
            got, _ = self.run_with(self.created)
        self.assertEqual(got.status, orch.OrchestrationStatus.RECONCILIATION_REQUIRED)
        # The tearDown patch raises if delete_voice_channel() is called.


if __name__ == "__main__":
    unittest.main()
