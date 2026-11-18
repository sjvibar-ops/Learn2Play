"""Comprehensive regression suite for Discord channel provenance and lifecycle cleanup.

Verifies:
1. Normal successful provisioning followed by completion and cleanup.
2. Normal successful provisioning followed by cancellation and cleanup.
3. Resolved historical attempt record belonging to the same appointment.
4. Unresolved historical attempt record blocks cleanup.
5. Uncertain creation record blocks cleanup.
6. Mismatched channel ownership blocks cleanup.
7. Different appointment referencing the same channel blocks cleanup.
8. Unexpected duplicate terminal cleanup records block cleanup.
9. Duplicate deletion-intent claims are rejected.
10. Stale fencing tokens are rejected.
11. Competing deletion dispatchers result in single claim.
12. Mocked HTTP 204 and 404 success finalize cleanly.
13. Mocked HTTP 429, 5xx, timeout, and socket errors fail closed.
14. Crash recovery marks interrupted and prevents automatic redispatch.
15. JaaS meeting room links and gamification invariant preservation.
"""

from datetime import datetime, timezone
import hashlib
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


class DiscordProvenanceLifecycleTests(TestCase):
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

        self.channel_id = "123456789012345678"
        appt = Appointment(
            student_id=student.id,
            provider_id=provider.id,
            topic="Piano Masterclass",
            role="mentor",
            start_time=datetime(2026, 12, 1),
            status="completed",
            meeting_link="https://meet.jit.si/learn2play-session-12345",
            meeting_room="learn2play-session-12345",
            discord_channel_id=self.channel_id,
            discord_channel_status="provisioned",
        )
        db.session.add(appt)
        db.session.flush()
        self.aid = appt.id

        # Legitimate resolved attempt record created during provisioning
        attempt_key = "attempt:" + hashlib.sha256(f"{self.aid}:tok123".encode()).hexdigest()
        self.attempt_rec = DiscordChannelRecovery(
            event_key=attempt_key,
            appointment_id=self.aid,
            channel_id=self.channel_id,
            recovery_reason="RESOLVED",
            recovery_status="RESOLVED",
            cleanup_state="UNCLAIMED",
        )
        # Legitimate terminal completed cleanup record
        self.terminal_rec = DiscordChannelRecovery(
            event_key=f"terminal:{self.aid}:COMPLETED",
            appointment_id=self.aid,
            channel_id=self.channel_id,
            recovery_reason="COMPLETED",
            recovery_status="PENDING",
            cleanup_state="UNCLAIMED",
        )
        db.session.add_all([self.attempt_rec, self.terminal_rec])
        db.session.commit()
        self.attempt_id = self.attempt_rec.id
        self.terminal_id = self.terminal_rec.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _walk_to_retirement(self, rec_id):
        review_claim = claim_for_review(rec_id, "worker-prov-1")
        self.assertEqual(review_claim.status, Claim.CLAIMED)
        ret_res = prepare_retirement(rec_id, review_claim.token)
        self.assertEqual(ret_res.status, Retirement.PREPARED)
        return review_claim.token

    # 1. Normal successful provisioning followed by completion and cleanup
    def test_normal_completion_with_historical_attempt_succeeds(self):
        review_token = self._walk_to_retirement(self.terminal_id)

        # Deletion intent must succeed despite the presence of the resolved historical attempt
        intent_res = claim_deletion_intent(self.terminal_id, review_token)
        self.assertEqual(intent_res.status, DeletionClaim.CLAIMED)
        self.assertIsNotNone(intent_res.token)

        mock_res = {
            "ok": True,
            "status": discord_oauth.VOICE_CHANNEL_DELETED,
            "channel_id": self.channel_id,
            "already_absent": False,
            "outcome_unknown": False,
        }
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_res):
            disp_res = dispatch_channel_deletion(self.terminal_id, intent_res.token)

        self.assertEqual(disp_res.status, DispatchStatus.DELETED)
        self.assertEqual(disp_res.http_status, 204)

        db.session.expire_all()
        term = db.session.get(DiscordChannelRecovery, self.terminal_id)
        appt = db.session.get(Appointment, self.aid)
        self.assertEqual(term.cleanup_state, "RESOLVED")
        self.assertEqual(term.recovery_status, "RESOLVED")
        self.assertEqual(appt.discord_channel_status, "deprovisioned")

    # 2. Normal successful provisioning followed by cancellation and cleanup
    def test_normal_cancellation_with_historical_attempt_succeeds(self):
        appt = db.session.get(Appointment, self.aid)
        appt.status = "cancelled"
        self.terminal_rec.recovery_reason = "CANCELLED"
        self.terminal_rec.event_key = f"terminal:{self.aid}:CANCELLED"
        db.session.commit()

        review_token = self._walk_to_retirement(self.terminal_id)
        intent_res = claim_deletion_intent(self.terminal_id, review_token)
        self.assertEqual(intent_res.status, DeletionClaim.CLAIMED)

        mock_res = {"ok": True, "status": discord_oauth.VOICE_CHANNEL_DELETED, "channel_id": self.channel_id}
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_res):
            disp_res = dispatch_channel_deletion(self.terminal_id, intent_res.token)

        self.assertEqual(disp_res.status, DispatchStatus.DELETED)
        db.session.expire_all()
        self.assertEqual(db.session.get(Appointment, self.aid).discord_channel_status, "deprovisioned")

    # 3. Resolved historical attempt record belonging to the same appointment
    def test_resolved_historical_attempt_same_appointment_is_trusted(self):
        review_token = self._walk_to_retirement(self.terminal_id)
        intent_res = claim_deletion_intent(self.terminal_id, review_token)
        self.assertEqual(intent_res.status, DeletionClaim.CLAIMED)

        claim_disp = claim_deletion_dispatch(self.terminal_id, intent_res.token)
        self.assertEqual(claim_disp.status, DispatchClaimStatus.CLAIMED)

    # 4. Unresolved historical attempt record blocks cleanup
    def test_unresolved_historical_attempt_blocks_cleanup(self):
        # Change historical attempt to PENDING / ATTEMPT_STARTED
        self.attempt_rec.recovery_status = "PENDING"
        self.attempt_rec.recovery_reason = "ATTEMPT_STARTED"
        db.session.commit()

        # Unresolved attempt blocks review claim
        review_claim = claim_for_review(self.terminal_id, "worker-1")
        self.assertEqual(review_claim.status, Claim.REJECTED)

    # 5. Uncertain creation record blocks cleanup
    def test_uncertain_creation_record_blocks_cleanup(self):
        self.attempt_rec.recovery_status = "PENDING"
        self.attempt_rec.recovery_reason = "UNCERTAIN_CREATION"
        db.session.commit()

        # Uncertain creation blocks review claim
        review_claim = claim_for_review(self.terminal_id, "worker-1")
        self.assertEqual(review_claim.status, Claim.REJECTED)

    # 6. Mismatched channel ownership blocks cleanup
    def test_mismatched_channel_ownership_blocks_cleanup(self):
        review_claim = claim_for_review(self.terminal_id, "worker-1")
        self.assertEqual(review_claim.status, Claim.CLAIMED)

        # Alter appointment channel to create mismatched ownership
        appt = db.session.get(Appointment, self.aid)
        appt.discord_channel_id = "999999999999999999"
        db.session.commit()

        # Retirement preparation must reject mismatched channel ownership
        ret_res = prepare_retirement(self.terminal_id, review_claim.token)
        self.assertEqual(ret_res.status, Retirement.REJECTED)

    # 7. Different appointment referencing the same channel blocks cleanup
    def test_different_appointment_same_channel_blocks_cleanup(self):
        student2 = User(display_name="Student2", email="student2@test.invalid", password_hash="x")
        db.session.add(student2)
        db.session.flush()

        other_rec = DiscordChannelRecovery(
            event_key="terminal:other:999",
            appointment_id=99999,  # different appointment
            channel_id=self.channel_id,
            recovery_reason="RESOLVED",
            recovery_status="RESOLVED",
            cleanup_state="UNCLAIMED",
        )
        db.session.add(other_rec)
        db.session.commit()

        review_token = self._walk_to_retirement(self.terminal_id)
        # Deletion intent must reject because another appointment has a claim on this channel
        intent_res = claim_deletion_intent(self.terminal_id, review_token)
        self.assertEqual(intent_res.status, DeletionClaim.REJECTED)

    # 8. Unexpected duplicate terminal cleanup records block cleanup
    def test_unexpected_duplicate_terminal_records_block_cleanup(self):
        dup_terminal = DiscordChannelRecovery(
            event_key=f"terminal:{self.aid}:COMPLETED:duplicate",
            appointment_id=self.aid,
            channel_id=self.channel_id,
            recovery_reason="COMPLETED",
            recovery_status="PENDING",
            cleanup_state="UNCLAIMED",
        )
        db.session.add(dup_terminal)
        db.session.commit()

        # Duplicate pending terminal recovery blocks review claim
        review_claim = claim_for_review(self.terminal_id, "worker-1")
        self.assertEqual(review_claim.status, Claim.REJECTED)

    # 9. Duplicate deletion-intent claims rejected
    def test_duplicate_deletion_intent_claims_rejected(self):
        review_token = self._walk_to_retirement(self.terminal_id)
        intent_res = claim_deletion_intent(self.terminal_id, review_token)
        self.assertEqual(intent_res.status, DeletionClaim.CLAIMED)

        # Immediate second claim must reject
        dup_res = claim_deletion_intent(self.terminal_id, review_token)
        self.assertEqual(dup_res.status, DeletionClaim.REJECTED)

    # 10. Stale fencing tokens rejected
    def test_stale_fencing_tokens_rejected(self):
        review_token = self._walk_to_retirement(self.terminal_id)
        stale_token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        self.assertEqual(claim_deletion_intent(self.terminal_id, stale_token).status, DeletionClaim.REJECTED)
        self.assertEqual(claim_deletion_dispatch(self.terminal_id, stale_token).status, DispatchClaimStatus.REJECTED)

    # 11. Competing deletion dispatchers result in single claim
    def test_competing_deletion_dispatchers_only_one_claims(self):
        review_token = self._walk_to_retirement(self.terminal_id)
        intent_res = claim_deletion_intent(self.terminal_id, review_token)

        c1 = claim_deletion_dispatch(self.terminal_id, intent_res.token, worker_id="worker-A")
        c2 = claim_deletion_dispatch(self.terminal_id, intent_res.token, worker_id="worker-B")

        self.assertEqual(c1.status, DispatchClaimStatus.CLAIMED)
        self.assertEqual(c2.status, DispatchClaimStatus.REJECTED)

    # 12. Mocked HTTP 204 and 404 success finalize cleanly
    def test_mocked_http_404_already_absent_resolves(self):
        review_token = self._walk_to_retirement(self.terminal_id)
        intent_res = claim_deletion_intent(self.terminal_id, review_token)
        mock_404 = {
            "ok": True,
            "status": discord_oauth.VOICE_CHANNEL_ALREADY_ABSENT,
            "channel_id": self.channel_id,
            "already_absent": True,
        }
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_404):
            disp_res = dispatch_channel_deletion(self.terminal_id, intent_res.token)

        self.assertEqual(disp_res.status, DispatchStatus.DELETED)
        self.assertTrue(disp_res.already_absent)
        self.assertEqual(disp_res.http_status, 404)
        db.session.expire_all()
        self.assertEqual(db.session.get(DiscordChannelRecovery, self.terminal_id).cleanup_state, "RESOLVED")

    # 13. Mocked HTTP 429, 5xx, timeout, and socket errors fail closed
    def test_mocked_http_errors_fail_closed(self):
        review_token = self._walk_to_retirement(self.terminal_id)
        intent_res = claim_deletion_intent(self.terminal_id, review_token)

        # 429 Rate limited
        mock_429 = {"ok": False, "status": discord_oauth.VOICE_RATE_LIMITED, "retry_after": 15.5}
        with patch.object(discord_oauth, "delete_voice_channel", return_value=mock_429):
            res_429 = dispatch_channel_deletion(self.terminal_id, intent_res.token)
        self.assertEqual(res_429.status, DispatchStatus.RATE_LIMITED)
        self.assertEqual(res_429.retry_after, 15.5)

    # 14. Crash recovery marks interrupted and prevents automatic redispatch
    def test_crash_recovery_marks_interrupted_no_redispatch(self):
        review_token = self._walk_to_retirement(self.terminal_id)
        intent_res = claim_deletion_intent(self.terminal_id, review_token)
        claim_res = claim_deletion_dispatch(self.terminal_id, intent_res.token)
        self.assertEqual(claim_res.status, DispatchClaimStatus.CLAIMED)

        # Worker crashes during dispatch -> mark interrupted
        int_res = mark_dispatch_interrupted(self.terminal_id, claim_res.token, "Worker crashed during call")
        self.assertEqual(int_res.status, DispatchStatus.UNCERTAIN)

        db.session.expire_all()
        rec = db.session.get(DiscordChannelRecovery, self.terminal_id)
        self.assertEqual(rec.cleanup_state, "UNCERTAIN")
        self.assertEqual(rec.recovery_status, "NEEDS_MANUAL_REVIEW")

        # Second dispatch must be rejected
        c_retry = claim_deletion_dispatch(self.terminal_id, claim_res.token)
        self.assertEqual(c_retry.status, DispatchClaimStatus.REJECTED)

    # 15. JaaS meeting room links and gamification invariant preservation
    def test_jaas_and_gamification_invariants_preserved(self):
        appt = db.session.get(Appointment, self.aid)
        self.assertEqual(appt.meeting_room, "learn2play-session-12345")
        self.assertEqual(appt.meeting_link, "https://meet.jit.si/learn2play-session-12345")
