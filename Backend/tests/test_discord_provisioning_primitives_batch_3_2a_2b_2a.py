"""Batch 3.2A-2B-2A: database-only, isolated provisioning primitive tests.

Normal tests use the guarded app factory with private in-memory SQLite.
Concurrency uses a temporary SQLite file outside the repository and separate
SQLAlchemy sessions per worker; no persistent app DB is accessed.
"""
import concurrent.futures
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.orm import scoped_session, sessionmaker

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import Appointment, User
    from Backend import discord_provisioning as provisioning
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import Appointment, User
    import discord_provisioning as provisioning


START = datetime(2026, 12, 1, 9, 0, 0)
CHANNEL_A = "123456789012345678"
CHANNEL_B = "123456789012345679"


class ProvisioningPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(db.engine.url.database, ":memory:")
        db.create_all()
        self.provider = User(display_name="Provider", email="p@provision.test", password_hash="test")
        self.student = User(display_name="Student", email="s@provision.test", password_hash="test")
        db.session.add_all([self.provider, self.student])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def appointment(self, **kwargs):
        defaults = dict(provider_id=self.provider.id, student_id=self.student.id,
                        role="mentor", topic="Provisioning test", start_time=START,
                        status="accepted")
        defaults.update(kwargs)
        a = Appointment(**defaults)
        db.session.add(a)
        db.session.commit()
        return a.id

    def state(self, appointment_id):
        db.session.expire_all()
        return db.session.get(Appointment, appointment_id)

    def claim(self, appointment_id):
        return provisioning.claim_provisioning(appointment_id)

    def test_successful_claim_and_token_persistence(self):
        aid = self.appointment()
        result = self.claim(aid)
        self.assertEqual(result.status, provisioning.ClaimStatus.CLAIMED)
        self.assertRegex(result.token, r"^[0-9a-f]{64}$")
        row = self.state(aid)
        self.assertEqual(row.discord_channel_attempt_token, result.token)
        self.assertEqual(row.discord_channel_status, "provisioning")
        self.assertIsNotNone(row.discord_channel_last_attempt_at)

    def test_token_uses_secrets(self):
        aid = self.appointment()
        with patch.object(provisioning.secrets, "token_hex", return_value="ab" * 32) as mock:
            result = self.claim(aid)
        mock.assert_called_once_with(32)
        self.assertEqual(result.token, "ab" * 32)

    def test_eligibility_rejections(self):
        for status in ("pending", "rejected", "cancelled", "completed"):
            with self.subTest(status=status):
                aid = self.appointment(status=status)
                self.assertEqual(self.claim(aid).status, provisioning.ClaimStatus.INELIGIBLE_BOOKING)
                self.assertEqual(self.state(aid).discord_channel_status, "unprovisioned")

    def test_missing_and_invalid_appointment_id(self):
        self.assertEqual(self.claim(999999).status, provisioning.ClaimStatus.NOT_FOUND)
        for invalid in (None, True, "1", 0, -1, 1.0):
            self.assertEqual(self.claim(invalid).status, provisioning.ClaimStatus.INVALID_INPUT)

    def test_repeat_claim_in_progress_does_not_leak_token(self):
        aid = self.appointment()
        self.claim(aid)
        result = self.claim(aid)
        self.assertEqual(result.status, provisioning.ClaimStatus.IN_PROGRESS)
        self.assertIsNone(result.token)

    def test_uncertain_and_stale_provisioning_blocked(self):
        for status in ("uncertain", "provisioning"):
            aid = self.appointment(discord_channel_status=status,
                                   discord_channel_last_attempt_at=datetime(2000, 1, 1))
            result = self.claim(aid)
            self.assertIn(result.status, (provisioning.ClaimStatus.UNCERTAIN_LOCKED,
                                          provisioning.ClaimStatus.IN_PROGRESS))
            self.assertEqual(self.state(aid).discord_channel_status, status)

    def test_deprovisioned_not_reclaimed(self):
        aid = self.appointment(discord_channel_status="deprovisioned")
        self.assertEqual(self.claim(aid).status, provisioning.ClaimStatus.CONCURRENT_CONFLICT)

    def test_already_provisioned_idempotency(self):
        aid = self.appointment(discord_channel_status="provisioned", discord_channel_id=CHANNEL_A)
        result = self.claim(aid)
        self.assertEqual(result.status, provisioning.ClaimStatus.ALREADY_PROVISIONED)
        self.assertEqual(result.existing_channel_id, CHANNEL_A)

    def test_failed_can_be_claimed_again(self):
        aid = self.appointment(discord_channel_status="failed")
        self.assertEqual(self.claim(aid).status, provisioning.ClaimStatus.CLAIMED)

    def test_success_finalization(self):
        aid = self.appointment()
        token = self.claim(aid).token
        result = provisioning.finalize_provisioning_success(aid, token, CHANNEL_A)
        self.assertEqual(result.status, provisioning.FinalizeStatus.FINALIZED)
        self.assertEqual(result.channel_id, CHANNEL_A)
        row = self.state(aid)
        self.assertEqual(row.discord_channel_id, CHANNEL_A)
        self.assertEqual(row.discord_channel_status, "provisioned")
        self.assertIsNone(row.discord_channel_attempt_token)
        self.assertIsNotNone(row.discord_channel_created_at)
        self.assertEqual(self.claim(aid).status, provisioning.ClaimStatus.ALREADY_PROVISIONED)

    def test_wrong_token_and_stale_token_rejected(self):
        aid = self.appointment()
        original = self.claim(aid).token
        self.assertEqual(provisioning.finalize_provisioning_success(aid, "ab" * 32, CHANNEL_A).status,
                         provisioning.FinalizeStatus.FENCING_REJECTED)
        db.session.execute(sa.update(Appointment).where(Appointment.id == aid).values(
            discord_channel_attempt_token="cd" * 32))
        db.session.commit()
        self.assertEqual(provisioning.finalize_provisioning_success(aid, original, CHANNEL_A).status,
                         provisioning.FinalizeStatus.FENCING_REJECTED)
        self.assertIsNone(self.state(aid).discord_channel_id)

    def test_booking_terminal_before_success_finalization(self):
        for status in ("cancelled", "completed"):
            aid = self.appointment()
            token = self.claim(aid).token
            db.session.execute(sa.update(Appointment).where(Appointment.id == aid).values(status=status))
            db.session.commit()
            result = provisioning.finalize_provisioning_success(aid, token, CHANNEL_A)
            self.assertEqual(result.status, provisioning.FinalizeStatus.FENCING_REJECTED)
            self.assertIsNone(self.state(aid).discord_channel_id)

    def test_duplicate_channel_constraint(self):
        first = self.appointment()
        token1 = self.claim(first).token
        self.assertEqual(provisioning.finalize_provisioning_success(first, token1, CHANNEL_A).status,
                         provisioning.FinalizeStatus.FINALIZED)
        second = self.appointment()
        token2 = self.claim(second).token
        result = provisioning.finalize_provisioning_success(second, token2, CHANNEL_A)
        self.assertEqual(result.status, provisioning.FinalizeStatus.INTEGRITY_CONFLICT)
        self.assertEqual(self.state(second).discord_channel_status, "provisioning")

    def test_invalid_token_channel_and_booking(self):
        aid = self.appointment()
        token = self.claim(aid).token
        for bad in ("ab" * 31, "z" * 64, token + "\n", None, 123):
            self.assertEqual(provisioning.finalize_provisioning_success(aid, bad, CHANNEL_A).status,
                             provisioning.FinalizeStatus.INVALID_INPUT)
        for bad in ("123", CHANNEL_A + "\n", "x" * 18, 123):
            self.assertEqual(provisioning.finalize_provisioning_success(aid, token, bad).status,
                             provisioning.FinalizeStatus.INVALID_INPUT)
        self.assertIsNone(self.state(aid).discord_channel_id)

    def test_definitive_failure_is_retryable(self):
        aid = self.appointment()
        token = self.claim(aid).token
        result = provisioning.finalize_provisioning_failure(aid, token, provisioning.FailureOutcome.NOT_CREATED)
        self.assertEqual(result.status, provisioning.FinalizeStatus.FINALIZED)
        self.assertEqual(self.state(aid).discord_channel_status, "failed")
        self.assertIsNone(self.state(aid).discord_channel_attempt_token)
        self.assertEqual(self.claim(aid).status, provisioning.ClaimStatus.CLAIMED)

    def test_unknown_failure_blocks_retry(self):
        aid = self.appointment()
        token = self.claim(aid).token
        result = provisioning.finalize_provisioning_failure(aid, token, provisioning.FailureOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(result.status, provisioning.FinalizeStatus.FINALIZED)
        self.assertEqual(self.state(aid).discord_channel_status, "uncertain")
        self.assertEqual(self.claim(aid).status, provisioning.ClaimStatus.UNCERTAIN_LOCKED)

    def test_invalid_failure_outcome_no_change(self):
        aid = self.appointment()
        token = self.claim(aid).token
        for bad in ("NOT_CREATED", "error", None, True, {}):
            self.assertEqual(provisioning.finalize_provisioning_failure(aid, token, bad).status,
                             provisioning.FinalizeStatus.INVALID_INPUT)
        self.assertEqual(self.state(aid).discord_channel_status, "provisioning")

    def test_failure_fencing_and_terminal_booking(self):
        aid = self.appointment()
        token = self.claim(aid).token
        self.assertEqual(provisioning.finalize_provisioning_failure(aid, "ef" * 32,
                         provisioning.FailureOutcome.NOT_CREATED).status,
                         provisioning.FinalizeStatus.FENCING_REJECTED)
        db.session.execute(sa.update(Appointment).where(Appointment.id == aid).values(status="cancelled"))
        db.session.commit()
        self.assertEqual(provisioning.finalize_provisioning_failure(aid, token,
                         provisioning.FailureOutcome.OUTCOME_UNKNOWN).status,
                         provisioning.FinalizeStatus.FINALIZED)
        self.assertEqual(self.state(aid).status, "cancelled")
        self.assertEqual(self.state(aid).discord_channel_status, "uncertain")

    def test_failure_never_modifies_existing_channel(self):
        aid = self.appointment()
        token = self.claim(aid).token
        db.session.execute(sa.update(Appointment).where(Appointment.id == aid).values(discord_channel_id=CHANNEL_A))
        db.session.commit()
        self.assertEqual(provisioning.finalize_provisioning_failure(aid, token,
                         provisioning.FailureOutcome.NOT_CREATED).status,
                         provisioning.FinalizeStatus.FENCING_REJECTED)

    def test_database_commit_failure_never_returns_claimed(self):
        aid = self.appointment()
        original_commit = db.session.commit
        with patch.object(db.session, "commit", side_effect=RuntimeError("s3cr3t db-url")):
            result = self.claim(aid)
        self.assertEqual(result.status, provisioning.ClaimStatus.DATABASE_ERROR)
        self.assertIsNone(result.token)
        self.assertNotIn("s3cr3t", repr(result))

    def test_finalization_commit_failure_does_not_claim_success(self):
        aid = self.appointment()
        token = self.claim(aid).token
        with patch.object(db.session, "commit", side_effect=RuntimeError("postgresql://secret")):
            result = provisioning.finalize_provisioning_success(aid, token, CHANNEL_A)
        self.assertEqual(result.status, provisioning.FinalizeStatus.DATABASE_ERROR)
        self.assertNotIn("postgresql", repr(result))

    def test_no_discord_http_usage(self):
        self.assertNotIn("discord_oauth", provisioning.__dict__)
        self.assertNotIn("urllib", provisioning.__dict__)
        self.assertNotIn("requests", provisioning.__dict__)


class SharedDatabaseConcurrencyTests(unittest.TestCase):
    """Independent threads, SQLAlchemy sessions and connections on a disposable file."""

    def test_simultaneous_claims_one_winner(self):
        with tempfile.TemporaryDirectory(prefix="l2p_discord_claim_") as folder:
            path = Path(folder) / "claims.sqlite3"
            self.assertNotEqual(str(path), "Backend/learn2play.db")
            engine = sa.create_engine("sqlite:///" + path.as_posix(),
                                      connect_args={"timeout": 10, "check_same_thread": False})
            # Use the current application's ORM metadata, but never the real app DB.
            Appointment.metadata.create_all(engine)
            Session = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))
            try:
                with Session() as session:
                    student = User(display_name="Student", email="thread-student@example.test", password_hash="test")
                    provider = User(display_name="Provider", email="thread-provider@example.test", password_hash="test")
                    session.add_all([student, provider]); session.flush()
                    appt = Appointment(provider_id=provider.id, student_id=student.id,
                                       role="mentor", topic="Thread test", start_time=START, status="accepted")
                    session.add(appt); session.commit()
                    aid = appt.id
                # Patch the domain's db session with a thread-local scoped session.
                fake_db = SimpleNamespace(session=Session)
                barrier = threading.Barrier(2)
                def worker():
                    barrier.wait(timeout=5)
                    try:
                        return provisioning.claim_provisioning(aid)
                    finally:
                        Session.remove()
                with patch.object(provisioning, "db", fake_db):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                        first = executor.submit(worker)
                        second = executor.submit(worker)
                        results = [first.result(timeout=12), second.result(timeout=12)]
                statuses = [result.status for result in results]
                self.assertEqual(statuses.count(provisioning.ClaimStatus.CLAIMED), 1)
                self.assertEqual(statuses.count(provisioning.ClaimStatus.IN_PROGRESS), 1)
                with Session() as session:
                    state = session.get(Appointment, aid)
                    self.assertEqual(state.discord_channel_status, "provisioning")
                    self.assertEqual(state.discord_channel_attempt_token,
                                     next(x.token for x in results if x.status == provisioning.ClaimStatus.CLAIMED))
            finally:
                Session.remove()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
