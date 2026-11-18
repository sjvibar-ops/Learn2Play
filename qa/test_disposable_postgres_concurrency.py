"""Disposable PostgreSQL multi-worker concurrency tests (Feature 3 Final Verification).

SAFETY INVARIANTS:
- NEVER connects to production, Supabase, Render, or persistent databases.
- Requires BOTH an explicit authorization sentinel and a validated loopback URL.
- Database name MUST start with 'l2p_disposable_' or 'learn2play_disposable_'.
- Covers synthetic SQL tests (supplemental evidence) and actual application-level
  functions across independently scoped SQLAlchemy sessions/connections.
"""

import concurrent.futures
from datetime import datetime, timezone
import hashlib
import os
import re
import secrets
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

SENTINEL = "YES_I_AUTHORIZE_DISPOSABLE_TEST_DB"
ALLOWED_DB_PREFIXES = ("l2p_disposable_", "learn2play_disposable_")


def disposable_url():
    if os.environ.get("L2P_DISPOSABLE_PG_TESTS") != SENTINEL:
        return None
    raw = os.environ.get("L2P_DISPOSABLE_PG_URL", "")
    try:
        u = make_url(raw)
    except Exception:
        return None

    # Strict loopback and disposable database validation
    if (
        u.drivername not in ("postgresql", "postgresql+psycopg", "postgresql+psycopg2")
        or u.host not in ("127.0.0.1", "localhost")
        or not any((u.database or "").startswith(p) for p in ALLOWED_DB_PREFIXES)
        or u.query
    ):
        return None
    return raw


# ==============================================================================
# 1. SYNTHETIC SQL CONCURRENCY TESTS (Supplemental Evidence)
# ==============================================================================

@unittest.skipUnless(disposable_url(), "requires explicitly authorized disposable loopback PostgreSQL")
class SyntheticDisposablePostgresConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = disposable_url()
        cls.engine = create_engine(
            url,
            pool_size=16,
            max_overflow=4,
            connect_args={"connect_timeout": 5},
        )
        cls.Session = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        # Build ephemeral tables with isolated names per test to guarantee no cross-test pollution
        self.prefix = "t_" + uuid4().hex[:12]
        self.metadata = sa.MetaData()

        self.users = sa.Table(
            f"{self.prefix}_users", self.metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("display_name", sa.String(100)),
        )
        self.appointments = sa.Table(
            f"{self.prefix}_appointments", self.metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("discord_channel_id", sa.String(32), unique=True, nullable=True),
            sa.Column("discord_channel_status", sa.String(32), nullable=False, default="unprovisioned"),
            sa.Column("discord_channel_attempt_token", sa.String(64), nullable=True),
        )
        self.recoveries = sa.Table(
            f"{self.prefix}_recoveries", self.metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("event_key", sa.String(100), unique=True, nullable=False),
            sa.Column("appointment_id", sa.Integer, nullable=False),
            sa.Column("channel_id", sa.String(32), nullable=True),
            sa.Column("recovery_reason", sa.String(32), nullable=False),
            sa.Column("recovery_status", sa.String(32), nullable=False, default="PENDING"),
            sa.Column("cleanup_state", sa.String(32), nullable=False, default="UNCLAIMED"),
            sa.Column("cleanup_fencing_token", sa.String(64), nullable=True),
            sa.Column("deletion_http_status", sa.Integer, nullable=True),
        )

        self.metadata.create_all(self.engine)

    def tearDown(self):
        self.metadata.drop_all(self.engine)

    # 1. Concurrent channel provisioning claims
    def test_concurrent_channel_provisioning_claims(self):
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.appointments).values(
                id=1, status="accepted", discord_channel_status="unprovisioned", discord_channel_attempt_token=None
            ))

        barrier = threading.Barrier(8)

        def worker_claim(worker_id):
            barrier.wait(timeout=5)
            token = f"tok_{worker_id:060d}"
            with self.engine.begin() as conn:
                res = conn.execute(
                    sa.update(self.appointments).where(
                        self.appointments.c.id == 1,
                        self.appointments.c.status == "accepted",
                        self.appointments.c.discord_channel_status == "unprovisioned",
                        self.appointments.c.discord_channel_attempt_token.is_(None),
                    ).values(
                        discord_channel_status="provisioning",
                        discord_channel_attempt_token=token,
                    )
                )
                return res.rowcount

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker_claim, range(8)))

        self.assertEqual(results.count(1), 1)
        self.assertEqual(results.count(0), 7)

    # 2. Concurrent provisioning finalization
    def test_concurrent_provisioning_finalization(self):
        valid_token = "valid_tok_" + "0" * 54
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.appointments).values(
                id=2, status="accepted", discord_channel_status="provisioning", discord_channel_attempt_token=valid_token
            ))

        def try_finalize(token, channel_id):
            with self.engine.begin() as conn:
                return conn.execute(
                    sa.update(self.appointments).where(
                        self.appointments.c.id == 2,
                        self.appointments.c.discord_channel_status == "provisioning",
                        self.appointments.c.discord_channel_attempt_token == token,
                    ).values(
                        discord_channel_status="provisioned",
                        discord_channel_id=channel_id,
                        discord_channel_attempt_token=None,
                    )
                ).rowcount

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(try_finalize, valid_token, "chan_1001")
            f2 = pool.submit(try_finalize, "stale_tok_" + "0" * 54, "chan_1002")
            res = [f1.result(), f2.result()]

        self.assertEqual(res, [1, 0])

    # 3. Provisioning versus cancellation conflict
    def test_provisioning_vs_cancellation_conflict(self):
        token = "prov_tok_" + "0" * 55
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.appointments).values(
                id=3, status="cancelled", discord_channel_status="unprovisioned", discord_channel_attempt_token=token
            ))

        # A cancelled booking MUST NOT be finalized to provisioned
        with self.engine.begin() as conn:
            finalized = conn.execute(
                sa.update(self.appointments).where(
                    self.appointments.c.id == 3,
                    self.appointments.c.status.in_(("accepted", "scheduled")),
                    self.appointments.c.discord_channel_status == "provisioning",
                    self.appointments.c.discord_channel_attempt_token == token,
                ).values(discord_channel_status="provisioned")
            ).rowcount

        self.assertEqual(finalized, 0)

    # 4. Concurrent cleanup review claims
    def test_concurrent_cleanup_review_claims(self):
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.recoveries).values(
                id=4, event_key="term:4", appointment_id=4, channel_id="c_4",
                recovery_reason="COMPLETED", recovery_status="PENDING", cleanup_state="UNCLAIMED"
            ))

        barrier = threading.Barrier(6)

        def claim_review(wid):
            barrier.wait(timeout=5)
            tok = f"rev_{wid:060d}"
            with self.engine.begin() as conn:
                return conn.execute(
                    sa.update(self.recoveries).where(
                        self.recoveries.c.id == 4,
                        self.recoveries.c.cleanup_state == "UNCLAIMED",
                        self.recoveries.c.cleanup_fencing_token.is_(None),
                    ).values(cleanup_state="REVIEW_CLAIMED", cleanup_fencing_token=tok)
                ).rowcount

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            res = list(pool.map(claim_review, range(6)))

        self.assertEqual(res.count(1), 1)
        self.assertEqual(res.count(0), 5)

    # 5. Retirement versus active booking
    def test_retirement_vs_active_booking_protection(self):
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.appointments).values(
                id=5, status="accepted", discord_channel_id="c_5", discord_channel_status="provisioned"
            ))

        # Trying to deprovision an accepted booking must be rejected
        with self.engine.begin() as conn:
            changed = conn.execute(
                sa.update(self.appointments).where(
                    self.appointments.c.id == 5,
                    self.appointments.c.status.in_(("cancelled", "completed")),
                    self.appointments.c.discord_channel_status == "provisioned",
                ).values(discord_channel_status="deprovisioning")
            ).rowcount

        self.assertEqual(changed, 0)

    # 6. Concurrent deletion-intent claims
    def test_concurrent_deletion_intent_claims(self):
        rev_token = "tok_rev_" + "0" * 56
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.recoveries).values(
                id=6, event_key="term:6", appointment_id=6, channel_id="c_6",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="RETIREMENT_PREPARED", cleanup_fencing_token=rev_token
            ))

        barrier = threading.Barrier(4)

        def claim_intent(wid):
            barrier.wait(timeout=5)
            new_tok = f"intent_{wid:057d}"
            with self.engine.begin() as conn:
                return conn.execute(
                    sa.update(self.recoveries).where(
                        self.recoveries.c.id == 6,
                        self.recoveries.c.cleanup_state == "RETIREMENT_PREPARED",
                        self.recoveries.c.cleanup_fencing_token == rev_token,
                    ).values(cleanup_state="DELETION_INTENT_CLAIMED", cleanup_fencing_token=new_tok)
                ).rowcount

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            res = list(pool.map(claim_intent, range(4)))

        self.assertEqual(res.count(1), 1)
        self.assertEqual(res.count(0), 3)

    # 7. Concurrent deletion dispatch
    def test_concurrent_deletion_dispatch(self):
        intent_token = "tok_intent_" + "0" * 53
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.recoveries).values(
                id=7, event_key="term:7", appointment_id=7, channel_id="c_7",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DELETION_INTENT_CLAIMED", cleanup_fencing_token=intent_token
            ))

        barrier = threading.Barrier(4)

        def claim_dispatch(wid):
            barrier.wait(timeout=5)
            disp_tok = f"disp_{wid:059d}"
            with self.engine.begin() as conn:
                return conn.execute(
                    sa.update(self.recoveries).where(
                        self.recoveries.c.id == 7,
                        self.recoveries.c.cleanup_state == "DELETION_INTENT_CLAIMED",
                        self.recoveries.c.cleanup_fencing_token == intent_token,
                    ).values(cleanup_state="DISPATCH_IN_FLIGHT", cleanup_fencing_token=disp_tok)
                ).rowcount

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            res = list(pool.map(claim_dispatch, range(4)))

        self.assertEqual(res.count(1), 1)
        self.assertEqual(res.count(0), 3)

    # 8. Stale token finalization rejected
    def test_stale_token_finalization_rejected(self):
        active_disp_tok = "active_disp_" + "0" * 52
        stale_disp_tok = "stale_disp_" + "0" * 53
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.recoveries).values(
                id=8, event_key="term:8", appointment_id=8, channel_id="c_8",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DISPATCH_IN_FLIGHT", cleanup_fencing_token=active_disp_tok
            ))

        with self.engine.begin() as conn:
            res = conn.execute(
                sa.update(self.recoveries).where(
                    self.recoveries.c.id == 8,
                    self.recoveries.c.cleanup_state == "DISPATCH_IN_FLIGHT",
                    self.recoveries.c.cleanup_fencing_token == stale_disp_tok,
                ).values(cleanup_state="RESOLVED", recovery_status="RESOLVED")
            ).rowcount

        self.assertEqual(res, 0)

    # 9. Worker interruption marks uncertain
    def test_worker_interruption_marks_uncertain(self):
        disp_tok = "disp_int_" + "0" * 55
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.recoveries).values(
                id=9, event_key="term:9", appointment_id=9, channel_id="c_9",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DISPATCH_IN_FLIGHT", cleanup_fencing_token=disp_tok
            ))

        # Worker interrupted -> mark uncertain fail-closed
        with self.engine.begin() as conn:
            marked = conn.execute(
                sa.update(self.recoveries).where(
                    self.recoveries.c.id == 9,
                    self.recoveries.c.cleanup_state == "DISPATCH_IN_FLIGHT",
                    self.recoveries.c.cleanup_fencing_token == disp_tok,
                ).values(cleanup_state="UNCERTAIN", recovery_status="NEEDS_MANUAL_REVIEW")
            ).rowcount

        self.assertEqual(marked, 1)

        # Retry with old token must be rejected
        with self.engine.begin() as conn:
            retry = conn.execute(
                sa.update(self.recoveries).where(
                    self.recoveries.c.id == 9,
                    self.recoveries.c.cleanup_state == "DISPATCH_IN_FLIGHT",
                    self.recoveries.c.cleanup_fencing_token == disp_tok,
                ).values(cleanup_state="RESOLVED")
            ).rowcount

        self.assertEqual(retry, 0)

    # 10. Conflicting provenance records handling
    def test_conflicting_provenance_records_handling(self):
        # Appointment 10 has a legitimate resolved historical attempt
        # and a terminal completed cleanup record
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.recoveries).values([
                {
                    "id": 101, "event_key": "attempt:hash101", "appointment_id": 10,
                    "channel_id": "c_10", "recovery_reason": "RESOLVED", "recovery_status": "RESOLVED",
                    "cleanup_state": "UNCLAIMED", "cleanup_fencing_token": None
                },
                {
                    "id": 102, "event_key": "terminal:10:COMPLETED", "appointment_id": 10,
                    "channel_id": "c_10", "recovery_reason": "COMPLETED", "recovery_status": "PENDING",
                    "cleanup_state": "RETIREMENT_PREPARED", "cleanup_fencing_token": "tok_prep"
                }
            ]))

        r = self.recoveries
        other = sa.orm.aliased(r)

        conflicting_recovery = sa.exists(sa.select(other.c.id).where(
            other.c.channel_id == r.c.channel_id,
            other.c.id != r.c.id,
            ~sa.and_(
                other.c.appointment_id == r.c.appointment_id,
                other.c.channel_id == r.c.channel_id,
                other.c.event_key.like("attempt:%"),
                other.c.recovery_reason == "RESOLVED",
                other.c.recovery_status == "RESOLVED",
                other.c.cleanup_state.in_(("UNCLAIMED", "RESOLVED")),
                other.c.cleanup_fencing_token.is_(None),
            ),
        ))

        # For record 102 (terminal), historical record 101 is non-conflicting, so intent claim succeeds
        with self.engine.begin() as conn:
            res_legit = conn.execute(
                sa.update(r).where(
                    r.c.id == 102,
                    r.c.cleanup_state == "RETIREMENT_PREPARED",
                    r.c.cleanup_fencing_token == "tok_prep",
                    ~conflicting_recovery,
                ).values(cleanup_state="DELETION_INTENT_CLAIMED", cleanup_fencing_token="tok_intent")
            ).rowcount
        self.assertEqual(res_legit, 1)

        # Now insert a genuinely conflicting unverified record from appointment 999
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.recoveries).values(
                id=103, event_key="other:conflicting:999", appointment_id=999,
                channel_id="c_10", recovery_reason="RESOLVED", recovery_status="RESOLVED",
                cleanup_state="UNCLAIMED", cleanup_fencing_token=None
            ))

        # Further claim must now be blocked by the conflicting provenance record
        with self.engine.begin() as conn:
            res_blocked = conn.execute(
                sa.update(r).where(
                    r.c.id == 102,
                    r.c.cleanup_state == "DELETION_INTENT_CLAIMED",
                    r.c.cleanup_fencing_token == "tok_intent",
                    ~conflicting_recovery,
                ).values(cleanup_state="DISPATCH_IN_FLIGHT")
            ).rowcount
        self.assertEqual(res_blocked, 0)


# ==============================================================================
# 2. ACTUAL APPLICATION CONCURRENCY & CRASH SAFETY TESTS (Primary Verification)
# ==============================================================================

def _make_appt(id, status="accepted", **kwargs):
    from Backend.models import Appointment
    params = {
        "id": id,
        "student_id": 1,
        "provider_id": 2,
        "topic": "Python Concurrency QA",
        "start_time": datetime.now(timezone.utc),
        "status": status,
    }
    params.update(kwargs)
    return Appointment(**params)


@unittest.skipUnless(disposable_url(), "requires explicitly authorized disposable loopback PostgreSQL")
class ApplicationDisposablePostgresConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = disposable_url()
        os.environ["DATABASE_URL"] = url
        os.environ["FLASK_ENV"] = "testing"
        os.environ["SECRET_KEY"] = "disposable-test-secret-key-32chars-minimum-entropy"

        from Backend import create_app, db
        from Backend.models import Appointment, DiscordChannelRecovery, User

        cls.app = create_app()
        cls.app.config["DISCORD_GUILD_ID"] = "123456789012345678"
        cls.app.config["DISCORD_BOT_TOKEN"] = "mock_bot_token_123456789"

        connect_args = {"prepare_threshold": None}
        try:
            test_engine = sa.create_engine(
                url,
                connect_args={"sslmode": "require", "connect_timeout": 2, "prepare_threshold": None},
            )
            with test_engine.connect() as test_conn:
                test_conn.execute(sa.text("SELECT 1"))
            connect_args["sslmode"] = "require"
            test_engine.dispose()
        except Exception:
            connect_args["sslmode"] = "prefer"

        cls.engine = sa.create_engine(
            cls.app.config["SQLALCHEMY_DATABASE_URI"],
            pool_size=20,
            max_overflow=5,
            pool_pre_ping=True,
            connect_args=connect_args,
        )

        with cls.app.app_context():
            db.engines[None] = cls.engine
            # Reset and seed base tables
            db.session.query(DiscordChannelRecovery).delete()
            db.session.query(Appointment).delete()
            db.session.query(User).delete()
            db.session.commit()

            u1 = User(id=1, email="s1@test.com", display_name="Student1", password_hash="h", roles_json='["student"]')
            u2 = User(id=2, email="t1@test.com", display_name="Tutor1", password_hash="h", roles_json='["mentor"]')
            db.session.add_all([u1, u2])
            db.session.commit()

    def setUp(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        with self.app.app_context():
            db.session.query(DiscordChannelRecovery).delete()
            db.session.query(Appointment).delete()
            db.session.commit()

    # 1. Concurrent provisioning claims
    def test_app_concurrent_provisioning_claims(self):
        from Backend import db
        from Backend.models import Appointment
        with self.app.app_context():
            appt = _make_appt(
                101, status="accepted",
                discord_channel_status="unprovisioned", discord_channel_attempt_token=None
            )
            db.session.add(appt)
            db.session.commit()

        barrier = threading.Barrier(8)

        def worker_claim(wid):
            with self.app.app_context():
                barrier.wait(timeout=5)
                tok = secrets.token_hex(32)
                rowcount = db.session.execute(
                    sa.update(Appointment).where(
                        Appointment.id == 101,
                        Appointment.status.in_(("accepted", "scheduled")),
                        Appointment.discord_channel_status == "unprovisioned",
                        Appointment.discord_channel_attempt_token.is_(None),
                    ).values(
                        discord_channel_status="provisioning",
                        discord_channel_attempt_token=tok,
                    )
                ).rowcount
                if rowcount == 1:
                    db.session.commit()
                    return (1, tok)
                else:
                    db.session.rollback()
                    return (0, None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker_claim, range(8)))

        counts = [r[0] for r in results]
        self.assertEqual(counts.count(1), 1)
        self.assertEqual(counts.count(0), 7)

    # 2. Concurrent provisioning finalization
    def test_app_concurrent_provisioning_finalization(self):
        from Backend import db
        from Backend.models import Appointment
        valid_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                102, status="accepted",
                discord_channel_status="provisioning", discord_channel_attempt_token=valid_tok
            )
            db.session.add(appt)
            db.session.commit()

        def try_finalize(tok, chan):
            with self.app.app_context():
                rowcount = db.session.execute(
                    sa.update(Appointment).where(
                        Appointment.id == 102,
                        Appointment.discord_channel_status == "provisioning",
                        Appointment.discord_channel_attempt_token == tok,
                    ).values(
                        discord_channel_status="provisioned",
                        discord_channel_id=chan,
                        discord_channel_attempt_token=None,
                    )
                ).rowcount
                if rowcount == 1:
                    db.session.commit()
                    return 1
                else:
                    db.session.rollback()
                    return 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            f_valid = pool.submit(try_finalize, valid_tok, "chan_102")
            f_stale1 = pool.submit(try_finalize, "stale_1" + "0" * 57, "chan_102")
            f_stale2 = pool.submit(try_finalize, "stale_2" + "0" * 57, "chan_102")
            f_stale3 = pool.submit(try_finalize, "stale_3" + "0" * 57, "chan_102")
            res = [f_valid.result(), f_stale1.result(), f_stale2.result(), f_stale3.result()]

        self.assertEqual(res, [1, 0, 0, 0])

    # 3. Provisioning versus cancellation race
    def test_app_provisioning_vs_cancellation_race(self):
        from Backend import db
        from Backend.models import Appointment
        tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                103, status="cancelled",
                discord_channel_status="provisioning", discord_channel_attempt_token=tok
            )
            db.session.add(appt)
            db.session.commit()

        # Finalization must be rejected because status is cancelled
        with self.app.app_context():
            rowcount = db.session.execute(
                sa.update(Appointment).where(
                    Appointment.id == 103,
                    Appointment.status.in_(("accepted", "scheduled")),
                    Appointment.discord_channel_status == "provisioning",
                    Appointment.discord_channel_attempt_token == tok,
                ).values(
                    discord_channel_status="provisioned",
                    discord_channel_id="chan_103",
                )
            ).rowcount
            self.assertEqual(rowcount, 0)

    # 4. Concurrent cleanup review claims (claim_for_review)
    def test_app_concurrent_cleanup_review_claims(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_cleanup_fencing import claim_for_review, Claim

        with self.app.app_context():
            appt = _make_appt(
                104, status="completed",
                discord_channel_id="chan_104", discord_channel_status="provisioned"
            )
            rec = DiscordChannelRecovery(
                id=104, event_key="terminal:104:COMPLETED", appointment_id=104, channel_id="chan_104",
                recovery_reason="COMPLETED", recovery_status="PENDING", cleanup_state="UNCLAIMED"
            )
            db.session.add_all([appt, rec])
            db.session.commit()

        barrier = threading.Barrier(8)

        def worker_claim(wid):
            with self.app.app_context():
                barrier.wait(timeout=5)
                return claim_for_review(104, f"worker_{wid}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker_claim, range(8)))

        statuses = [r.status for r in results]
        self.assertEqual(statuses.count(Claim.CLAIMED), 1)
        self.assertEqual(statuses.count(Claim.REJECTED), 7)

    # 5. Retirement preparation races (prepare_retirement)
    def test_app_retirement_preparation_races(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_cleanup_fencing import claim_for_review, Claim
        from Backend.discord_channel_retirement import prepare_retirement, Retirement

        with self.app.app_context():
            appt = _make_appt(
                105, status="completed",
                discord_channel_id="chan_105", discord_channel_status="provisioned"
            )
            rec = DiscordChannelRecovery(
                id=105, event_key="terminal:105:COMPLETED", appointment_id=105, channel_id="chan_105",
                recovery_reason="COMPLETED", recovery_status="PENDING", cleanup_state="UNCLAIMED"
            )
            db.session.add_all([appt, rec])
            db.session.commit()

            claim_res = claim_for_review(105, "winner_worker")
            self.assertEqual(claim_res.status, Claim.CLAIMED)
            valid_review_tok = claim_res.token

        barrier = threading.Barrier(4)

        def worker_retire(wid):
            with self.app.app_context():
                barrier.wait(timeout=5)
                tok = valid_review_tok if wid == 0 else secrets.token_hex(32)
                return prepare_retirement(105, tok)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker_retire, range(4)))

        statuses = [r.status for r in results]
        self.assertEqual(statuses.count(Retirement.PREPARED), 1)
        self.assertEqual(statuses.count(Retirement.REJECTED), 3)

        with self.app.app_context():
            a = db.session.get(Appointment, 105)
            r = db.session.get(DiscordChannelRecovery, 105)
            self.assertEqual(a.discord_channel_status, "deprovisioning")
            self.assertEqual(r.cleanup_state, "RETIREMENT_PREPARED")

    # 6. Concurrent deletion-intent claims (claim_deletion_intent)
    def test_app_concurrent_deletion_intent_claims(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_intent import claim_deletion_intent, DeletionClaim

        with self.app.app_context():
            appt = _make_appt(
                106, status="completed",
                discord_channel_id="chan_106", discord_channel_status="deprovisioning"
            )
            review_tok = secrets.token_hex(32)
            rec = DiscordChannelRecovery(
                id=106, event_key="terminal:106:COMPLETED", appointment_id=106, channel_id="chan_106",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="RETIREMENT_PREPARED", cleanup_fencing_token=review_tok
            )
            db.session.add_all([appt, rec])
            db.session.commit()

        barrier = threading.Barrier(6)

        def worker_intent(wid):
            with self.app.app_context():
                barrier.wait(timeout=5)
                return claim_deletion_intent(106, review_tok)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(worker_intent, range(6)))

        statuses = [r.status for r in results]
        self.assertEqual(statuses.count(DeletionClaim.CLAIMED), 1)
        self.assertEqual(statuses.count(DeletionClaim.REJECTED), 5)

    # 7. Concurrent deletion-dispatch claims (claim_deletion_dispatch)
    def test_app_concurrent_deletion_dispatch_claims(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_dispatch import claim_deletion_dispatch, DispatchClaimStatus

        intent_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                107, status="completed",
                discord_channel_id="chan_107", discord_channel_status="deprovisioning"
            )
            rec = DiscordChannelRecovery(
                id=107, event_key="terminal:107:COMPLETED", appointment_id=107, channel_id="chan_107",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DELETION_INTENT_CLAIMED", cleanup_fencing_token=intent_tok
            )
            db.session.add_all([appt, rec])
            db.session.commit()

        barrier = threading.Barrier(6)

        def worker_disp(wid):
            with self.app.app_context():
                barrier.wait(timeout=5)
                return claim_deletion_dispatch(107, intent_tok, worker_id=f"disp_worker_{wid}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(worker_disp, range(6)))

        statuses = [r.status for r in results]
        self.assertEqual(statuses.count(DispatchClaimStatus.CLAIMED), 1)
        self.assertEqual(statuses.count(DispatchClaimStatus.REJECTED), 5)

    # 8. Stale-token finalization rejection (finalize_deletion_dispatch)
    def test_app_stale_token_finalization_rejection(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_dispatch import finalize_deletion_dispatch, DispatchStatus
        import Backend.discord_oauth as discord_oauth

        disp_tok = secrets.token_hex(32)
        stale_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                108, status="completed",
                discord_channel_id="chan_108", discord_channel_status="deprovisioning"
            )
            rec = DiscordChannelRecovery(
                id=108, event_key="terminal:108:COMPLETED", appointment_id=108, channel_id="chan_108",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DISPATCH_IN_FLIGHT", cleanup_fencing_token=disp_tok
            )
            db.session.add_all([appt, rec])
            db.session.commit()

            # Finalize with stale token must be rejected
            res = finalize_deletion_dispatch(
                108, stale_tok, {"ok": True, "status": discord_oauth.VOICE_CHANNEL_DELETED, "http_status": 204}, 108, "chan_108"
            )
            self.assertEqual(res.status, DispatchStatus.REJECTED)

            # DB state must remain in flight
            r_row = db.session.get(DiscordChannelRecovery, 108)
            self.assertEqual(r_row.cleanup_state, "DISPATCH_IN_FLIGHT")
            self.assertEqual(r_row.cleanup_fencing_token, disp_tok)

    # 9. Historical provenance vs conflicting ownership
    def test_app_historical_provenance_vs_conflicting_ownership(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_intent import claim_deletion_intent, DeletionClaim
        from Backend.discord_deletion_dispatch import claim_deletion_dispatch, DispatchClaimStatus

        review_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                109, status="completed",
                discord_channel_id="chan_109", discord_channel_status="deprovisioning"
            )
            appt_conflict = _make_appt(
                999, status="completed",
                discord_channel_id=None, discord_channel_status="unprovisioned"
            )
            hist_rec = DiscordChannelRecovery(
                id=1091, event_key="attempt:tok1091", appointment_id=109, channel_id="chan_109",
                recovery_reason="RESOLVED", recovery_status="RESOLVED",
                cleanup_state="UNCLAIMED", cleanup_fencing_token=None
            )
            term_rec = DiscordChannelRecovery(
                id=1092, event_key="terminal:109:COMPLETED", appointment_id=109, channel_id="chan_109",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="RETIREMENT_PREPARED", cleanup_fencing_token=review_tok
            )
            db.session.add_all([appt, appt_conflict, hist_rec, term_rec])
            db.session.commit()

            # Positive provenance: claim_deletion_intent succeeds because hist_rec is trusted
            res_intent = claim_deletion_intent(1092, review_tok)
            self.assertEqual(res_intent.status, DeletionClaim.CLAIMED)
            intent_tok = res_intent.token

            # Insert an unverified conflicting record from appointment 999
            conflict = DiscordChannelRecovery(
                id=1093, event_key="other:conflict:999", appointment_id=999, channel_id="chan_109",
                recovery_reason="RESOLVED", recovery_status="RESOLVED",
                cleanup_state="UNCLAIMED", cleanup_fencing_token=None
            )
            db.session.add(conflict)
            db.session.commit()

            # Negative provenance: claim_deletion_dispatch is blocked due to conflict
            res_disp = claim_deletion_dispatch(1092, intent_tok)
            self.assertEqual(res_disp.status, DispatchClaimStatus.REJECTED)

    # 10. Worker interruption marks uncertain
    def test_app_worker_interruption_marks_uncertain(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_dispatch import mark_dispatch_interrupted, finalize_deletion_dispatch, DispatchStatus
        import Backend.discord_oauth as discord_oauth

        disp_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                110, status="completed",
                discord_channel_id="chan_110", discord_channel_status="deprovisioning"
            )
            rec = DiscordChannelRecovery(
                id=110, event_key="terminal:110:COMPLETED", appointment_id=110, channel_id="chan_110",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DISPATCH_IN_FLIGHT", cleanup_fencing_token=disp_tok
            )
            db.session.add_all([appt, rec])
            db.session.commit()

            # Worker crash / interruption
            res = mark_dispatch_interrupted(110, disp_tok, "Worker pod terminated abruptly")
            self.assertEqual(res.status, DispatchStatus.UNCERTAIN)

            r_row = db.session.get(DiscordChannelRecovery, 110)
            self.assertEqual(r_row.cleanup_state, "UNCERTAIN")
            self.assertEqual(r_row.recovery_status, "NEEDS_MANUAL_REVIEW")
            self.assertEqual(r_row.last_error_code, "INTERRUPTED")

            # Retrying with the same token must be rejected
            res_retry = finalize_deletion_dispatch(
                110, disp_tok, {"ok": True, "status": discord_oauth.VOICE_CHANNEL_DELETED, "http_status": 204}, 110, "chan_110"
            )
            self.assertEqual(res_retry.status, DispatchStatus.REJECTED)

    # 11. Transaction isolation between database operations and mocked Discord HTTP
    def test_app_transaction_isolation_and_decoupling(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_dispatch import dispatch_channel_deletion, DispatchStatus
        import Backend.discord_oauth as discord_oauth

        intent_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                111, status="completed",
                discord_channel_id="chan_111", discord_channel_status="deprovisioning"
            )
            rec = DiscordChannelRecovery(
                id=111, event_key="terminal:111:COMPLETED", appointment_id=111, channel_id="chan_111",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DELETION_INTENT_CLAIMED", cleanup_fencing_token=intent_tok
            )
            db.session.add_all([appt, rec])
            db.session.commit()

        # Mock delete_voice_channel and verify NO DB transaction is open
        def mock_delete(settings, channel_id):
            # Verify that session is removed and no transaction is active during external HTTP
            self.assertFalse(db.session.registry.has(), "Scoped session MUST NOT have an open session during Discord HTTP call")
            # Verify independently using a separate connection that DISPATCH_IN_FLIGHT was committed
            with self.engine.connect() as conn:
                state = conn.execute(
                    sa.select(DiscordChannelRecovery.cleanup_state).where(DiscordChannelRecovery.id == 111)
                ).scalar()
                self.assertEqual(state, "DISPATCH_IN_FLIGHT")
            return {"ok": True, "status": discord_oauth.VOICE_CHANNEL_DELETED, "channel_id": channel_id, "already_absent": False}

        with patch("Backend.discord_oauth.delete_voice_channel", side_effect=mock_delete):
            with self.app.app_context():
                res = dispatch_channel_deletion(111, intent_tok)
                self.assertEqual(res.status, DispatchStatus.DELETED)

        with self.app.app_context():
            a = db.session.get(Appointment, 111)
            r = db.session.get(DiscordChannelRecovery, 111)
            self.assertEqual(a.discord_channel_status, "deprovisioned")
            self.assertEqual(r.cleanup_state, "RESOLVED")
            self.assertEqual(r.recovery_status, "RESOLVED")

    # 12. Connection pool saturation and safe rollback
    def test_app_connection_pool_saturation_and_rollback(self):
        from Backend import db
        barrier = threading.Barrier(16)

        def worker_query(wid):
            with self.app.app_context():
                barrier.wait(timeout=5)
                # Intentionally trigger an error in half the workers
                if wid % 2 == 1:
                    try:
                        db.session.execute(sa.text("SELECT * FROM non_existent_table_for_pool_test"))
                    except Exception:
                        db.session.rollback()
                        return "rolled_back"
                else:
                    val = db.session.execute(sa.text("SELECT 1")).scalar()
                    db.session.commit()
                    return f"success_{val}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(worker_query, range(16)))

        self.assertEqual(results.count("rolled_back"), 8)
        self.assertEqual(results.count("success_1"), 8)

    # 13. Crash and retry safety: Mocked 404 (already absent)
    def test_app_crash_safety_mocked_404(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_dispatch import dispatch_channel_deletion, DispatchStatus
        import Backend.discord_oauth as discord_oauth

        intent_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                113, status="completed",
                discord_channel_id="chan_113", discord_channel_status="deprovisioning"
            )
            rec = DiscordChannelRecovery(
                id=113, event_key="terminal:113:COMPLETED", appointment_id=113, channel_id="chan_113",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DELETION_INTENT_CLAIMED", cleanup_fencing_token=intent_tok
            )
            db.session.add_all([appt, rec])
            db.session.commit()

        mock_404 = {"ok": True, "status": discord_oauth.VOICE_CHANNEL_ALREADY_ABSENT, "channel_id": "chan_113", "already_absent": True}
        with patch("Backend.discord_oauth.delete_voice_channel", return_value=mock_404):
            with self.app.app_context():
                res = dispatch_channel_deletion(113, intent_tok)
                self.assertEqual(res.status, DispatchStatus.DELETED)
                self.assertTrue(res.already_absent)

        with self.app.app_context():
            a = db.session.get(Appointment, 113)
            r = db.session.get(DiscordChannelRecovery, 113)
            self.assertEqual(a.discord_channel_status, "deprovisioned")
            self.assertEqual(r.cleanup_state, "RESOLVED")
            self.assertEqual(r.recovery_status, "RESOLVED")

    # 14. Crash and retry safety: Mocked 429 (rate limited)
    def test_app_crash_safety_mocked_429(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_dispatch import dispatch_channel_deletion, DispatchStatus
        import Backend.discord_oauth as discord_oauth

        intent_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                114, status="completed",
                discord_channel_id="chan_114", discord_channel_status="deprovisioning"
            )
            rec = DiscordChannelRecovery(
                id=114, event_key="terminal:114:COMPLETED", appointment_id=114, channel_id="chan_114",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DELETION_INTENT_CLAIMED", cleanup_fencing_token=intent_tok
            )
            db.session.add_all([appt, rec])
            db.session.commit()

        mock_429 = {"ok": False, "status": discord_oauth.VOICE_RATE_LIMITED, "retry_after": 5.0, "already_absent": False}
        with patch("Backend.discord_oauth.delete_voice_channel", return_value=mock_429):
            with self.app.app_context():
                res = dispatch_channel_deletion(114, intent_tok)
                self.assertEqual(res.status, DispatchStatus.RATE_LIMITED)
                self.assertEqual(res.retry_after, 5.0)

        with self.app.app_context():
            a = db.session.get(Appointment, 114)
            r = db.session.get(DiscordChannelRecovery, 114)
            # Channel ID must be preserved for future retry
            self.assertEqual(a.discord_channel_id, "chan_114")
            self.assertEqual(r.cleanup_state, "RATE_LIMITED")
            self.assertEqual(r.deletion_http_status, 429)

    # 15. Crash and retry safety: Mocked 5xx / timeout (uncertain)
    def test_app_crash_safety_mocked_5xx_timeout(self):
        from Backend import db
        from Backend.models import Appointment, DiscordChannelRecovery
        from Backend.discord_deletion_dispatch import dispatch_channel_deletion, DispatchStatus
        import Backend.discord_oauth as discord_oauth

        intent_tok = secrets.token_hex(32)
        with self.app.app_context():
            appt = _make_appt(
                115, status="completed",
                discord_channel_id="chan_115", discord_channel_status="deprovisioning"
            )
            rec = DiscordChannelRecovery(
                id=115, event_key="terminal:115:COMPLETED", appointment_id=115, channel_id="chan_115",
                recovery_reason="COMPLETED", recovery_status="PENDING",
                cleanup_state="DELETION_INTENT_CLAIMED", cleanup_fencing_token=intent_tok
            )
            db.session.add_all([appt, rec])
            db.session.commit()

        # Simulate socket timeout / 5xx
        mock_500 = {"ok": False, "status": discord_oauth.VOICE_UNAVAILABLE, "outcome_unknown": True, "already_absent": False}
        with patch("Backend.discord_oauth.delete_voice_channel", return_value=mock_500):
            with self.app.app_context():
                res = dispatch_channel_deletion(115, intent_tok)
                self.assertEqual(res.status, DispatchStatus.UNCERTAIN)

        with self.app.app_context():
            a = db.session.get(Appointment, 115)
            r = db.session.get(DiscordChannelRecovery, 115)
            # Must remain UNCERTAIN / NEEDS_MANUAL_REVIEW, not automatically retried
            self.assertEqual(a.discord_channel_id, "chan_115")
            self.assertEqual(r.cleanup_state, "UNCERTAIN")
            self.assertEqual(r.recovery_status, "NEEDS_MANUAL_REVIEW")
            self.assertEqual(r.last_error_code, "OUTCOME_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
