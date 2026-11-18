"""Batch 3.2A-2A: Appointment Discord channel schema, model and migration tests.

Every test here uses a fresh in-memory SQLite database (private to the test)
or no database at all. Nothing opens Backend/learn2play.db, the demo database,
the isolated live Discord test database or Supabase, and nothing contacts
Discord. The migration is only ever executed against a scratch in-memory
database that this module builds itself; no Alembic command (upgrade,
downgrade, stamp, autogenerate) is run against any existing database.
"""
import importlib.util
import unittest
import warnings
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError, SAWarning
from sqlalchemy.pool import StaticPool

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import Appointment, DISCORD_CHANNEL_STATUSES, User
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import Appointment, DISCORD_CHANNEL_STATUSES, User


NEW_REVISION = "e5a1b9c3d4f8"
PARENT_REVISION = "d8f3a1b72c49"
# Current migration head. NEW_REVISION above stays the 2A migration under test.
HEAD_REVISION = "d0a1b2c3d4e5"
LIFECYCLE_VALUES = (
    "unprovisioned",
    "provisioning",
    "provisioned",
    "failed",
    "uncertain",
    "deprovisioning",
    "deprovisioned",
)
CHANNEL_INDEX = "ix_appointments_discord_channel_id"

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
VERSIONS_DIR = MIGRATIONS_DIR / "versions"

START_TIME = datetime(2026, 12, 1, 9, 0, 0)  # naive UTC


def _script_directory():
    """Read the migration scripts without running env.py or touching a database."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config)


def _load_new_migration_module():
    matches = sorted(VERSIONS_DIR.glob(NEW_REVISION + "_*.py"))
    assert len(matches) == 1, matches
    spec = importlib.util.spec_from_file_location("migration_" + NEW_REVISION, matches[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AppointmentDiscordChannelModelTests(unittest.TestCase):
    def setUp(self):
        # create_app validates test_config BEFORE any engine exists and only
        # accepts the verified in-memory SQLite URI.
        self.app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        })
        self.context = self.app.app_context()
        self.context.push()
        self._assert_in_memory_sqlite_engine()
        db.create_all()

        self.provider = User(
            display_name="Channel Provider", email="provider@example.test",
            password_hash="test", roles_json='["mentor"]',
        )
        self.student = User(
            display_name="Channel Student", email="student@example.test",
            password_hash="test", roles_json='["student"]',
        )
        db.session.add_all([self.provider, self.student])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _assert_in_memory_sqlite_engine(self):
        self.assertEqual(db.engine.url.drivername, "sqlite")
        self.assertEqual(db.engine.url.database, ":memory:")
        with db.engine.connect() as connection:
            database_files = [
                row[2] for row in connection.exec_driver_sql("PRAGMA database_list").fetchall()
            ]
        self.assertEqual(database_files, [""])

    def _appointment(self, **overrides):
        values = dict(
            provider_id=self.provider.id,
            student_id=self.student.id,
            role="mentor",
            topic="Channel test session",
            start_time=START_TIME,
            duration_minutes=60,
            status="accepted",
        )
        values.update(overrides)
        return Appointment(**values)

    def _save(self, **overrides):
        appointment = self._appointment(**overrides)
        db.session.add(appointment)
        db.session.commit()
        return appointment

    def _reload(self, appointment_id):
        db.session.expire_all()
        return db.session.get(Appointment, appointment_id)

    # 1
    def test_new_appointment_defaults_to_unprovisioned(self):
        appointment = self._save()
        self.assertEqual(self._reload(appointment.id).discord_channel_status, "unprovisioned")

    # 2
    def test_new_appointment_channel_id_is_nullable_and_null(self):
        appointment = self._save()
        self.assertIsNone(self._reload(appointment.id).discord_channel_id)
        self.assertTrue(Appointment.__table__.c.discord_channel_id.nullable)

    # 3
    def test_new_appointment_channel_timestamps_are_nullable_and_null(self):
        appointment = self._save()
        reloaded = self._reload(appointment.id)
        self.assertIsNone(reloaded.discord_channel_created_at)
        self.assertIsNone(reloaded.discord_channel_last_attempt_at)
        self.assertTrue(Appointment.__table__.c.discord_channel_created_at.nullable)
        self.assertTrue(Appointment.__table__.c.discord_channel_last_attempt_at.nullable)

    # 4
    def test_valid_channel_id_is_stored_as_a_string(self):
        channel_id = "1234567890123456789"  # 19-digit snowflake
        created = datetime(2026, 12, 1, 8, 0, 0)
        attempt = datetime(2026, 12, 1, 7, 59, 0)
        appointment = self._save(
            discord_channel_id=channel_id,
            discord_channel_created_at=created,
            discord_channel_last_attempt_at=attempt,
            discord_channel_status="provisioned",
        )
        reloaded = self._reload(appointment.id)
        self.assertIsInstance(reloaded.discord_channel_id, str)
        self.assertEqual(reloaded.discord_channel_id, channel_id)
        self.assertEqual(reloaded.discord_channel_created_at, created)
        self.assertEqual(reloaded.discord_channel_last_attempt_at, attempt)
        self.assertEqual(reloaded.discord_channel_status, "provisioned")

    # 5
    def test_duplicate_non_null_channel_id_violates_uniqueness(self):
        self._save(discord_channel_id="111111111111111111")
        db.session.add(self._appointment(topic="Second", discord_channel_id="111111111111111111"))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    # 6
    def test_multiple_appointments_may_have_null_channel_ids(self):
        for index in range(3):
            self._save(topic="Null channel %d" % index)
        null_count = Appointment.query.filter(Appointment.discord_channel_id.is_(None)).count()
        self.assertEqual(null_count, 3)

    # 7
    def test_all_lifecycle_values_fit_the_column(self):
        self.assertEqual(tuple(DISCORD_CHANNEL_STATUSES), LIFECYCLE_VALUES)
        column = Appointment.__table__.c.discord_channel_status
        self.assertEqual(column.type.length, 20)
        for value in LIFECYCLE_VALUES:
            self.assertLessEqual(len(value), 20)
            appointment = self._save(topic="Status " + value, discord_channel_status=value)
            self.assertEqual(self._reload(appointment.id).discord_channel_status, value)

    # 8
    def test_existing_booking_fields_remain_functional(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SAWarning)  # SQLite Decimal notice
            appointment = self._save(
                price=Decimal("450.50"), location_type="Online", notes="Bring notes",
                meeting_link="https://example.test/room",
            )
            reloaded = self._reload(appointment.id)
            self.assertEqual(Decimal(str(reloaded.price)), Decimal("450.50"))
        self.assertEqual(reloaded.currency, "PHP")
        self.assertEqual(reloaded.role, "mentor")
        self.assertEqual(reloaded.topic, "Channel test session")
        self.assertEqual(reloaded.start_time, START_TIME)
        self.assertEqual(reloaded.duration_minutes, 60)
        self.assertEqual(reloaded.status, "accepted")
        self.assertEqual(reloaded.location_type, "Online")
        self.assertEqual(reloaded.notes, "Bring notes")
        self.assertIsNotNone(reloaded.created_at)
        self.assertIsNotNone(reloaded.updated_at)

        # Changing the booking status does not touch the Discord fields.
        reloaded.status = "completed"
        db.session.commit()
        again = self._reload(appointment.id)
        self.assertEqual(again.status, "completed")
        self.assertEqual(again.discord_channel_status, "unprovisioned")
        self.assertIsNone(again.discord_channel_id)

        # The booking status default is unchanged.
        defaulted = Appointment(
            provider_id=self.provider.id, student_id=self.student.id, topic="Defaults",
            start_time=START_TIME,
        )
        db.session.add(defaulted)
        db.session.commit()
        self.assertEqual(self._reload(defaulted.id).status, "scheduled")

    # 9
    def test_meeting_room_uniqueness_is_unchanged(self):
        self.assertTrue(Appointment.__table__.c.meeting_room.unique)
        self._save(meeting_room="l2-room-a")
        db.session.add(self._appointment(topic="Clash", meeting_room="l2-room-a"))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        # Several NULL meeting rooms remain allowed.
        self._save(topic="No room 1")
        self._save(topic="No room 2")

        # meeting_room and discord_channel_id are independent unique columns.
        self._save(topic="Both", meeting_room="l2-room-b", discord_channel_id="222222222222222222")

    # 10
    def test_user_appointment_relationships_remain_intact(self):
        appointment = self._save(discord_channel_id="333333333333333333")
        reloaded = self._reload(appointment.id)
        self.assertEqual(reloaded.provider.id, self.provider.id)
        self.assertEqual(reloaded.student.id, self.student.id)
        provider = db.session.get(User, self.provider.id)
        student = db.session.get(User, self.student.id)
        self.assertEqual([a.id for a in provider.provider_appointments], [appointment.id])
        self.assertEqual([a.id for a in student.student_appointments], [appointment.id])

    # 11
    def test_database_server_default_is_unprovisioned(self):
        column = Appointment.__table__.c.discord_channel_status
        self.assertFalse(column.nullable)
        self.assertEqual(str(column.server_default.arg), "unprovisioned")

        reflected = {
            c["name"]: c for c in sa.inspect(db.engine).get_columns("appointments")
        }["discord_channel_status"]
        self.assertFalse(reflected["nullable"])
        self.assertIn("unprovisioned", str(reflected["default"]))

        # A row inserted WITHOUT the column (as a pre-migration row would be)
        # receives the database default, not a Python-side one.
        db.session.execute(sa.text(
            "INSERT INTO appointments (provider_id, student_id, role, topic, start_time, "
            "duration_minutes, status, currency, created_at, updated_at) VALUES "
            "(:p, :s, 'mentor', 'Raw insert', '2026-12-01 09:00:00.000000', 60, 'accepted', "
            "'PHP', '2026-11-16 00:00:00.000000', '2026-11-16 00:00:00.000000')"
        ), {"p": self.provider.id, "s": self.student.id})
        db.session.commit()
        status = db.session.execute(sa.text(
            "SELECT discord_channel_status FROM appointments WHERE topic = 'Raw insert'"
        )).scalar_one()
        self.assertEqual(status, "unprovisioned")

    def test_channel_id_has_a_unique_index(self):
        indexes = {
            index["name"]: index for index in sa.inspect(db.engine).get_indexes("appointments")
        }
        self.assertIn(CHANNEL_INDEX, indexes)
        self.assertTrue(indexes[CHANNEL_INDEX]["unique"])
        self.assertEqual(indexes[CHANNEL_INDEX]["column_names"], ["discord_channel_id"])
        self.assertEqual(Appointment.__table__.c.discord_channel_id.type.length, 32)


class MigrationGraphTests(unittest.TestCase):
    """Reads the revision scripts only; no database is involved."""

    # 12
    def test_new_migration_depends_on_d8f3a1b72c49(self):
        script = _script_directory()
        revision = script.get_revision(NEW_REVISION)
        self.assertIsNotNone(revision)
        self.assertEqual(revision.down_revision, PARENT_REVISION)
        self.assertIsNone(revision.branch_labels or None)
        module = _load_new_migration_module()
        self.assertEqual(module.revision, NEW_REVISION)
        self.assertEqual(module.down_revision, PARENT_REVISION)

    # 13
    def test_migration_graph_has_one_linear_head(self):
        script = _script_directory()
        self.assertEqual(list(script.get_heads()), [HEAD_REVISION])
        self.assertEqual(len(script.get_bases()), 1)

        revisions = list(script.walk_revisions())
        self.assertEqual(revisions[0].revision, HEAD_REVISION)
        parents = []
        for revision in revisions:
            # A tuple down_revision would mean a merge point (non-linear).
            self.assertNotIsInstance(revision.down_revision, (tuple, list))
            if revision.down_revision is not None:
                parents.append(revision.down_revision)
        # Every revision except the base is the parent of exactly one child.
        self.assertEqual(len(parents), len(set(parents)))
        self.assertEqual(len(parents), len(revisions) - 1)

        # No revision ID is used by more than one script file.
        files = [p for p in VERSIONS_DIR.glob("*.py") if p.name != "__init__.py"]
        self.assertEqual(len(files), len(revisions))
        self.assertEqual(len([p for p in files if p.name.startswith(NEW_REVISION + "_")]), 1)

    def test_migration_source_stays_within_the_appointments_additions(self):
        source = next(VERSIONS_DIR.glob(NEW_REVISION + "_*.py")).read_text(encoding="utf-8")
        self.assertEqual(source.count("batch_alter_table('appointments'"), 2)
        self.assertNotIn("drop_table", source)
        self.assertNotIn("create_table", source)
        self.assertNotIn("drop_constraint", source)
        self.assertNotIn("meeting_room", source)


class MigrationOnScratchDatabaseTests(unittest.TestCase):
    """Runs the new migration on a private in-memory database built here.

    The scratch ``appointments`` table imitates the schema at d8f3a1b72c49 and
    holds pre-existing rows. This never touches an existing database.
    """

    NEW_COLUMNS = {
        "discord_channel_id",
        "discord_channel_created_at",
        "discord_channel_status",
        "discord_channel_last_attempt_at",
    }

    def setUp(self):
        self.engine = sa.create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.conn = self.engine.connect()
        self.conn.exec_driver_sql(
            "CREATE TABLE appointments ("
            "id INTEGER NOT NULL, "
            "provider_id INTEGER NOT NULL, "
            "student_id INTEGER NOT NULL, "
            "role VARCHAR(20) NOT NULL, "
            "topic VARCHAR(120) NOT NULL, "
            "start_time DATETIME NOT NULL, "
            "duration_minutes INTEGER NOT NULL, "
            "status VARCHAR(20) NOT NULL, "
            "currency VARCHAR(10) NOT NULL, "
            "meeting_room VARCHAR(80), "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, "
            "PRIMARY KEY (id), "
            "CONSTRAINT uq_appointments_meeting_room UNIQUE (meeting_room))"
        )
        for index, room in enumerate(("room-1", "room-2", None), start=1):
            self.conn.execute(sa.text(
                "INSERT INTO appointments (id, provider_id, student_id, role, topic, start_time, "
                "duration_minutes, status, currency, meeting_room, created_at, updated_at) VALUES "
                "(:id, 1, 2, 'mentor', 'Legacy', '2026-12-01 09:00:00.000000', 60, 'scheduled', "
                "'PHP', :room, '2026-11-01 00:00:00.000000', '2026-11-01 00:00:00.000000')"
            ), {"id": index, "room": room})
        self.conn.commit()
        self.module = _load_new_migration_module()

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def _run(self, function):
        context = MigrationContext.configure(self.conn)
        with Operations.context(context):
            function()
        self.conn.commit()

    def _column_names(self):
        return {c["name"] for c in sa.inspect(self.conn).get_columns("appointments")}

    def test_upgrade_preserves_rows_and_adds_only_the_new_columns(self):
        before = self._column_names()
        self._run(self.module.upgrade)

        self.assertEqual(self._column_names() - before, self.NEW_COLUMNS)
        self.assertTrue(before <= self._column_names())

        rows = self.conn.execute(sa.text(
            "SELECT id, topic, meeting_room, discord_channel_id, discord_channel_created_at, "
            "discord_channel_status, discord_channel_last_attempt_at "
            "FROM appointments ORDER BY id"
        )).all()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row.topic, "Legacy")
            self.assertIsNone(row.discord_channel_id)
            self.assertIsNone(row.discord_channel_created_at)
            self.assertIsNone(row.discord_channel_last_attempt_at)
            self.assertEqual(row.discord_channel_status, "unprovisioned")
        self.assertEqual([r.meeting_room for r in rows], ["room-1", "room-2", None])

        indexes = {i["name"]: i for i in sa.inspect(self.conn).get_indexes("appointments")}
        self.assertTrue(indexes[CHANNEL_INDEX]["unique"])
        self.assertEqual(indexes[CHANNEL_INDEX]["column_names"], ["discord_channel_id"])

        # The unique index allows many NULLs but rejects a duplicate ID.
        self.conn.execute(sa.text(
            "UPDATE appointments SET discord_channel_id = '444444444444444444' WHERE id = 1"
        ))
        with self.assertRaises(IntegrityError):
            self.conn.execute(sa.text(
                "UPDATE appointments SET discord_channel_id = '444444444444444444' WHERE id = 2"
            ))
        self.conn.rollback()

    def test_downgrade_removes_only_the_additions(self):
        before = self._column_names()
        self._run(self.module.upgrade)
        self._run(self.module.downgrade)

        self.assertEqual(self._column_names(), before)
        index_names = {i["name"] for i in sa.inspect(self.conn).get_indexes("appointments")}
        self.assertNotIn(CHANNEL_INDEX, index_names)
        count = self.conn.execute(sa.text("SELECT COUNT(*) FROM appointments")).scalar_one()
        self.assertEqual(count, 3)

        # The pre-existing meeting_room uniqueness survives the table rebuild.
        with self.assertRaises(IntegrityError):
            self.conn.execute(sa.text(
                "UPDATE appointments SET meeting_room = 'room-1' WHERE id = 2"
            ))
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
