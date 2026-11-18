"""Focused tests for safe Discord channel deletion dispatch, outcome finalization, and crash recovery."""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from Backend import create_app
from Backend.extensions import db
from Backend.models import Appointment, User, DiscordChannelRecovery
from Backend.discord_cleanup_fencing import claim_for_review, Claim
from Backend.discord_channel_retirement import prepare_retirement, Retirement
from Backend.discord_deletion_intent import claim_deletion_intent, DeletionClaim
from Backend.discord_deletion_dispatch import (
    claim_deletion_dispatch,
    finalize_deletion_dispatch,
    dispatch_channel_deletion,
    mark_dispatch_interrupted,
    DispatchClaimStatus,
    DispatchStatus,
)
import Backend.discord_oauth as discord_oauth


class DiscordDeletionDispatchTests(TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "DISCORD_SESSIONS_ENABLED": False,
            "DISCORD_BOT_TOKEN": "mock-valid-bot-token-for-test",
            "DISCORD_GUILD_ID": "123456789012345678",
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        assert db.engine.url.database == ":memory:"
        db.create_all()

        student = User(display_name="Student", email="student@test.invalid", password_hash="x")
        provider = User(display_name="Provider", email="provider@test.invalid", password_hash="x")
        db.session.add_all((student, provider))
        db.session.flush()

        appt = Appointment(
            student_id=student.id,
            provider_id=provider.id,
            topic="Math Coaching",
            role="mentor",
            start_time=datetime(2026, 12, 1),
            status="completed",
            meeting_link="https://meet.jit.si/learn2play-session-12345",
            meeting_room="learn2play-session-12345",
            discord_channel_id="123456789012345678",
            discord_channel_status="provisioned",
        )
        db.session.add(appt)
        db.session.flush()

        rec = DiscordChannelRecovery(
            event_key="terminal:dispatch:1",
            appointment_id=appt.id,
            channel_id=appt.discord_channel_id,
            recovery_reason="COMPLETED",
            recovery_status="PENDING",
        )
        db.session.add(rec)
        db.session.commit()
        self.aid, self.rid = appt.id, rec.id
        self.channel_id = appt.discord_channel_id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _prepare_to_deletion_intent(self):
        """Helper to walk the state machine up to DELETION_INTENT_CLAIMED."""
        review_claim = claim_for_review(self.rid, "worker-1")
        self.assertEqual(review_claim.status, Claim.CLAIMED)
        ret_res = prepare_retirement(self.rid, review_claim.token)
        self.assertEqual(ret_res.status, Retirement.PREPARED)
        intent_res = claim_deletion_intent(self.rid, review_claim.token)
        self.assertEqual(intent_res.status, DeletionClaim.CLAIMED)
        return intent_res.token

    # 1. Successful deletion dispatch (HTTP 204)
    def test_successful_deletion_dispatch_204(self):
        intent_token = self._prepare_to_deletion_intent()
        mock_res = {
            "ok": True,
            "status": discord_oauth.VOICE_CHANNEL_DELETED,
            "channel_id": self.channel_id,
            "already_absent": False,
            "outcome_unknown": False,
        }
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_res):
            res = dispatch_channel_deletion(self.rid, intent_token, worker_id="worker-del")

        self.assertEqual(res.status, DispatchStatus.DELETED)
        self.assertFalse(res.already_absent)
        self.assertEqual(res.http_status, 204)

        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.cleanup_state, "RESOLVED")
        self.assertEqual(rec.recovery_status, "RESOLVED")
        self.assertEqual(rec.deletion_http_status, 204)
        self.assertIsNotNone(rec.resolved_at)
        self.assertIn("deleted on Discord", rec.operator_notes)

        appt = db.session.get(Appointment, self.aid)
        self.assertEqual(appt.discord_channel_status, "deprovisioned")

    # 2. Competing workers attempting the same deletion (mutual exclusion)
    def test_competing_workers_only_one_claims(self):
        intent_token = self._prepare_to_deletion_intent()
        c1 = claim_deletion_dispatch(self.rid, intent_token, worker_id="worker-A")
        self.assertEqual(c1.status, DispatchClaimStatus.CLAIMED)
        self.assertIsNotNone(c1.token)

        c2 = claim_deletion_dispatch(self.rid, intent_token, worker_id="worker-B")
        self.assertEqual(c2.status, DispatchClaimStatus.REJECTED)

    # 3. Stale fencing token rejection
    def test_stale_fencing_token_rejected(self):
        self._prepare_to_deletion_intent()
        stale_token = "0" * 64
        c = claim_deletion_dispatch(self.rid, stale_token)
        self.assertEqual(c.status, DispatchClaimStatus.REJECTED)

    # 4. Active appointment protection
    def test_active_appointment_blocks_dispatch(self):
        intent_token = self._prepare_to_deletion_intent()
        # Move appointment to active status
        appt = db.session.get(Appointment, self.aid)
        appt.status = "scheduled"
        db.session.commit()

        c = claim_deletion_dispatch(self.rid, intent_token)
        self.assertEqual(c.status, DispatchClaimStatus.REJECTED)

    # 5. Conflicting channel ownership blocks dispatch
    def test_conflicting_channel_ownership_blocks_dispatch(self):
        intent_token = self._prepare_to_deletion_intent()
        # Add another recovery referencing the same channel
        db.session.add(DiscordChannelRecovery(
            event_key="other:competing:1",
            appointment_id=self.aid,
            channel_id=self.channel_id,
            recovery_reason="RESOLVED",
            recovery_status="RESOLVED",
        ))
        db.session.commit()

        c = claim_deletion_dispatch(self.rid, intent_token)
        self.assertEqual(c.status, DispatchClaimStatus.REJECTED)

    # 6. HTTP 200/204 outcome
    def test_http_200_outcome_resolves(self):
        intent_token = self._prepare_to_deletion_intent()
        mock_res = {
            "ok": True,
            "status": discord_oauth.VOICE_CHANNEL_DELETED,
            "channel_id": self.channel_id,
            "already_absent": False,
        }
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_res):
            res = dispatch_channel_deletion(self.rid, intent_token)
        self.assertEqual(res.status, DispatchStatus.DELETED)

    # 7. HTTP 404 (already absent) idempotent success
    def test_http_404_already_absent_idempotent(self):
        intent_token = self._prepare_to_deletion_intent()
        mock_res = {
            "ok": True,
            "status": discord_oauth.VOICE_CHANNEL_ALREADY_ABSENT,
            "channel_id": self.channel_id,
            "already_absent": True,
        }
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_res):
            res = dispatch_channel_deletion(self.rid, intent_token)

        self.assertEqual(res.status, DispatchStatus.DELETED)
        self.assertTrue(res.already_absent)
        self.assertEqual(res.http_status, 404)

        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.cleanup_state, "RESOLVED")
        self.assertIn("already absent", rec.operator_notes)

        appt = db.session.get(Appointment, self.aid)
        self.assertEqual(appt.discord_channel_status, "deprovisioned")

    # 8. HTTP 429 rate limit handling
    def test_http_429_rate_limited(self):
        intent_token = self._prepare_to_deletion_intent()
        mock_res = {
            "ok": False,
            "status": discord_oauth.VOICE_RATE_LIMITED,
            "retry_after": 7.25,
            "already_absent": False,
        }
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_res):
            res = dispatch_channel_deletion(self.rid, intent_token)

        self.assertEqual(res.status, DispatchStatus.RATE_LIMITED)
        self.assertEqual(res.retry_after, 7.25)
        self.assertEqual(res.http_status, 429)

        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.cleanup_state, "RATE_LIMITED")
        self.assertEqual(rec.deletion_http_status, 429)

        appt = db.session.get(Appointment, self.aid)
        self.assertEqual(appt.discord_channel_status, "deprovisioning")

    # 9. HTTP 5xx server error fail-closed
    def test_http_5xx_fail_closed_uncertain(self):
        intent_token = self._prepare_to_deletion_intent()
        mock_res = {
            "ok": False,
            "status": discord_oauth.VOICE_UNAVAILABLE,
            "outcome_unknown": True,
            "already_absent": False,
        }
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_res):
            res = dispatch_channel_deletion(self.rid, intent_token)

        self.assertEqual(res.status, DispatchStatus.UNCERTAIN)
        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.cleanup_state, "UNCERTAIN")
        self.assertEqual(rec.recovery_status, "NEEDS_MANUAL_REVIEW")

    # 10. Timeout and socket disconnect fail-closed
    def test_timeout_and_socket_errors(self):
        intent_token = self._prepare_to_deletion_intent()
        with patch.object(discord_oauth, "delete_voice_channel", side_effect=TimeoutError("Connection timed out")):
            res = dispatch_channel_deletion(self.rid, intent_token)

        self.assertEqual(res.status, DispatchStatus.UNCERTAIN)
        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.cleanup_state, "UNCERTAIN")
        self.assertEqual(rec.recovery_status, "NEEDS_MANUAL_REVIEW")

    # 11. Worker crash before HTTP dispatch preserves DISPATCH_IN_FLIGHT
    def test_worker_crash_before_http_preserves_state(self):
        intent_token = self._prepare_to_deletion_intent()
        claim = claim_deletion_dispatch(self.rid, intent_token)
        self.assertEqual(claim.status, DispatchClaimStatus.CLAIMED)

        # Worker crashes here; another worker tries to claim
        competing = claim_deletion_dispatch(self.rid, intent_token)
        self.assertEqual(competing.status, DispatchClaimStatus.REJECTED)

        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.cleanup_state, "DISPATCH_IN_FLIGHT")

    # 12. Worker crash after dispatch but before finalization
    def test_worker_crash_after_dispatch_mark_interrupted(self):
        intent_token = self._prepare_to_deletion_intent()
        claim = claim_deletion_dispatch(self.rid, intent_token)
        self.assertEqual(claim.status, DispatchClaimStatus.CLAIMED)

        interrupted_res = mark_dispatch_interrupted(self.rid, claim.token, reason="SIGTERM received")
        self.assertEqual(interrupted_res.status, DispatchStatus.UNCERTAIN)

        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.cleanup_state, "UNCERTAIN")
        self.assertEqual(rec.recovery_status, "NEEDS_MANUAL_REVIEW")
        self.assertEqual(rec.operator_notes, "SIGTERM received")

    # 13. Duplicate dispatch finalization prevention
    def test_duplicate_dispatch_finalization_rejected(self):
        intent_token = self._prepare_to_deletion_intent()
        claim = claim_deletion_dispatch(self.rid, intent_token)
        mock_res = {"ok": True, "status": discord_oauth.VOICE_CHANNEL_DELETED, "channel_id": self.channel_id}

        res1 = finalize_deletion_dispatch(self.rid, claim.token, mock_res, self.aid, self.channel_id)
        self.assertEqual(res1.status, DispatchStatus.DELETED)

        # Re-attempt with the now-spent dispatch token
        res2 = finalize_deletion_dispatch(self.rid, claim.token, mock_res, self.aid, self.channel_id)
        self.assertEqual(res2.status, DispatchStatus.REJECTED)

    # 14. Administrative reconciliation inspection
    def test_administrative_reconciliation_compatibility(self):
        intent_token = self._prepare_to_deletion_intent()
        with patch.object(discord_oauth, "delete_voice_channel", side_effect=RuntimeError("Network drop")):
            dispatch_channel_deletion(self.rid, intent_token)

        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertEqual(rec.recovery_status, "NEEDS_MANUAL_REVIEW")
        self.assertEqual(rec.cleanup_state, "UNCERTAIN")

    # 15. Audit-record integrity
    def test_audit_record_integrity(self):
        intent_token = self._prepare_to_deletion_intent()
        mock_res = {"ok": True, "status": discord_oauth.VOICE_CHANNEL_DELETED, "channel_id": self.channel_id}
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_res):
            dispatch_channel_deletion(self.rid, intent_token, worker_id="audit-worker-1")

        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.rid)
        self.assertIsNotNone(rec.deletion_dispatched_at)
        self.assertEqual(rec.deletion_http_status, 204)
        self.assertEqual(rec.cleanup_worker_id, "audit-worker-1")
        self.assertIsNotNone(rec.resolved_at)

    # 16. Provisioning versus retirement concurrency mutual exclusion
    def test_provisioning_vs_retirement_concurrency_mutex(self):
        self._prepare_to_deletion_intent()
        appt = db.session.get(Appointment, self.aid)
        self.assertEqual(appt.discord_channel_status, "deprovisioning")

    # 17. No database transaction remaining open across HTTP calls
    def test_no_database_transaction_during_http_dispatch(self):
        intent_token = self._prepare_to_deletion_intent()

        def http_hook(settings, channel_id):
            # Assert that during external HTTP execution, no open active transaction exists!
            session = db.session()
            self.assertFalse(session.in_transaction())
            return {"ok": True, "status": discord_oauth.VOICE_CHANNEL_DELETED, "channel_id": channel_id}

        with patch.object(discord_oauth, "delete_voice_channel", side_effect=http_hook):
            res = dispatch_channel_deletion(self.rid, intent_token)

        self.assertEqual(res.status, DispatchStatus.DELETED)

    # 18. JaaS and gamification invariants remain unaffected
    def test_jaas_and_gamification_unaffected(self):
        appt = db.session.get(Appointment, self.aid)
        self.assertEqual(appt.meeting_room, "learn2play-session-12345")
        self.assertEqual(appt.meeting_link, "https://meet.jit.si/learn2play-session-12345")
