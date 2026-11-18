"""Batch 3.2A-2B-1: Appointment Discord provisioning attempt token tests.

Schema only. Every test here uses a fresh in-memory SQLite database (private to
the test) or no database at all. Nothing opens Backend/learn2play.db, the demo
database, the isolated live Discord test database or Supabase, and nothing
contacts Discord. The migration is only ever executed against a scratch
in-memory database that this module builds itself; no Alembic command
(upgrade, downgrade, stamp, autogenerate) is run against any existing database.

No claim, fencing or concurrency behavior is tested because none exists yet.
"""
import ast
import importlib.util
import secrets
import unittest
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import Appointment, DISCORD_CHANNEL_STATUSES, User
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import Appointment, DISCORD_CHANNEL_STATUSES, User


NEW_REVISION = "f1a8c4d2e9b0"
PARENT_REVISION = "e5a1b9c3d4f8"
# Newest first, exactly as Alembic walks the history from the head.
EXPECTED_CHAIN = (
    "d0a1b2c3d4e5",
    "c9e0f1a2b3d4",
    "b8d9e0f1a2c3",
    "a7c8e9d0f1b2",
    "f1a8c4d2e9b0",
    "e5a1b9c3d4f8",
    "d8f3a1b72c49",
    "c4a7d91e5b26",
    "9e45f7e7aa32",
    "0720c7d1e4ca",
)
TOKEN_COLUMN = "discord_channel_attempt_token"
CHANNEL_INDEX = "ix_appointments_discord_channel_id"
MEETING_ROOM_CONSTRAINT = "uq_appointments_meeting_room"
LIFECYCLE_VALUES = (
    "unprovisioned",
    "provisioning",
    "provisioned",
    "failed",
    "uncertain",
    "deprovisioning",
    "deprovisioned",
)
# Appointment columns that existed before this batch.
EXISTING_COLUMNS = {
    "id", "provider_id", "student_id", "role", "topic", "start_time",
    "duration_minutes", "status", "price", "currency", "location_type",
    "meeting_link", "notes", "meeting_room", "discord_channel_id",
    "discord_channel_created_at", "discord_channel_status",
    "discord_channel_last_attempt_at", "created_at", "updated_at",
}

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
VERSIONS_DIR = MIGRATIONS_DIR / "versions"

START_TIME = datetime(2026, 12, 1, 9, 0, 0)  # naive UTC


def _script_directory():
    """Read the migration scripts without running env.py or touching a database."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config)


def _new_migration_path():
    matches = sorted(VERSIONS_DIR.glob(NEW_REVISION + "_*.py"))
    assert len(matches) == 1, matches
    return matches[0]


def _load_new_migration_module():
    path = _new_migration_path()
    spec = importlib.util.spec_from_file_location("migration_" + NEW_REVISION, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AppointmentAttemptTokenModelTests(unittest.TestCase):
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
            display_name="Token Provider", email="provider@example.test",
            password_hash="test", roles_json='["mentor"]',
        )
        self.student = User(
            display_name="Token Student", email="student@example.test",
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
            topic="Attempt token test session",
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
    def test_new_appointment_has_null_attempt_token(self):
        appointment = self._save()
        self.assertIsNone(self._reload(appointment.id).discord_channel_attempt_token)

        # Nothing generates or assigns a token automatically.
        column = Appointment.__table__.c[TOKEN_COLUMN]
        self.assertIsNone(column.default)
        self.assertIsNone(column.server_default)
        reloaded = self._reload(appointment.id)
        reloaded.status = "completed"
        reloaded.discord_channel_status = "provisioning"
        db.session.commit()
        self.assertIsNone(self._reload(appointment.id).discord_channel_attempt_token)

    # 2
    def test_token_accepts_a_valid_64_character_hex_string(self):
        for token in ("ab" * 32, secrets.token_hex(32)):
            self.assertEqual(len(token), 64)
            int(token, 16)  # valid hexadecimal
            appointment = self._save(topic="Token " + token[:8], discord_channel_attempt_token=token)
            reloaded = self._reload(appointment.id)
            self.assertIsInstance(reloaded.discord_channel_attempt_token, str)
            self.assertEqual(reloaded.discord_channel_attempt_token, token)

    # 3
    def test_token_field_is_a_nullable_string_of_at_most_64_characters(self):
        column = Appointment.__table__.c[TOKEN_COLUMN]
        self.assertTrue(column.nullable)
        self.assertIsInstance(column.type, sa.String)
        self.assertEqual(column.type.length, 64)

        reflected = {
            c["name"]: c for c in sa.inspect(db.engine).get_columns("appointments")
        }[TOKEN_COLUMN]
        self.assertTrue(reflected["nullable"])
        self.assertEqual(reflected["type"].length, 64)

        # Explicit NULL is stored and read back as NULL.
        appointment = self._save(discord_channel_attempt_token=None)
        self.assertIsNone(self._reload(appointment.id).discord_channel_attempt_token)

    # 4
    def test_token_can_be_updated(self):
        first, second = "11" * 32, "22" * 32
        appointment = self._save(discord_channel_attempt_token=first)
        self.assertEqual(self._reload(appointment.id).discord_channel_attempt_token, first)

        reloaded = self._reload(appointment.id)
        reloaded.discord_channel_attempt_token = second
        db.session.commit()
        self.assertEqual(self._reload(appointment.id).discord_channel_attempt_token, second)

    # 5
    def test_token_can_be_cleared_to_null(self):
        appointment = self._save(discord_channel_attempt_token="33" * 32)
        reloaded = self._reload(appointment.id)
        reloaded.discord_channel_attempt_token = None
        db.session.commit()
        self.assertIsNone(self._reload(appointment.id).discord_channel_attempt_token)

    # 6
    def test_existing_channel_fields_remain_intact(self):
        created = datetime(2026, 12, 1, 8, 0, 0)
        attempt = datetime(2026, 12, 1, 7, 59, 0)
        appointment = self._save(
            discord_channel_id="555555555555555555",
            discord_channel_created_at=created,
            discord_channel_status="provisioning",
            discord_channel_last_attempt_at=attempt,
            discord_channel_attempt_token="44" * 32,
        )

        def assert_channel_fields(row):
            self.assertEqual(row.discord_channel_id, "555555555555555555")
            self.assertEqual(row.discord_channel_created_at, created)
            self.assertEqual(row.discord_channel_status, "provisioning")
            self.assertEqual(row.discord_channel_last_attempt_at, attempt)

        reloaded = self._reload(appointment.id)
        assert_channel_fields(reloaded)
        self.assertEqual(reloaded.discord_channel_attempt_token, "44" * 32)

        # Changing the token leaves the channel fields alone.
        reloaded.discord_channel_attempt_token = "55" * 32
        db.session.commit()
        assert_channel_fields(self._reload(appointment.id))

        # Changing a channel field leaves the token alone.
        reloaded = self._reload(appointment.id)
        reloaded.discord_channel_status = "provisioned"
        db.session.commit()
        again = self._reload(appointment.id)
        self.assertEqual(again.discord_channel_status, "provisioned")
        self.assertEqual(again.discord_channel_attempt_token, "55" * 32)

        # The column definitions from Batch 3.2A-2A are unchanged.
        table = Appointment.__table__.c
        self.assertEqual(table.discord_channel_id.type.length, 32)
        self.assertTrue(table.discord_channel_id.nullable)
        self.assertTrue(table.discord_channel_created_at.nullable)
        self.assertTrue(table.discord_channel_last_attempt_at.nullable)
        self.assertEqual(table.discord_channel_status.type.length, 20)
        self.assertFalse(table.discord_channel_status.nullable)
        self.assertEqual(str(table.discord_channel_status.server_default.arg), "unprovisioned")
        self.assertEqual(tuple(DISCORD_CHANNEL_STATUSES), LIFECYCLE_VALUES)
        self.assertTrue(EXISTING_COLUMNS <= set(Appointment.__table__.c.keys()))

    # 7
    def test_appointment_status_is_independent_of_the_token(self):
        appointment = self._save()
        reloaded = self._reload(appointment.id)
        reloaded.discord_channel_attempt_token = "66" * 32
        db.session.commit()
        after_token = self._reload(appointment.id)
        self.assertEqual(after_token.status, "accepted")
        self.assertEqual(after_token.discord_channel_status, "unprovisioned")

        after_token.status = "completed"
        db.session.commit()
        completed = self._reload(appointment.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.discord_channel_attempt_token, "66" * 32)

        completed.status = "cancelled"
        completed.discord_channel_attempt_token = None
        db.session.commit()
        cancelled = self._reload(appointment.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(cancelled.discord_channel_attempt_token)
        self.assertEqual(cancelled.discord_channel_status, "unprovisioned")

        # The booking status default is unchanged.
        defaulted = Appointment(
            provider_id=self.provider.id, student_id=self.student.id, topic="Defaults",
            start_time=START_TIME,
        )
        db.session.add(defaulted)
        db.session.commit()
        self.assertEqual(self._reload(defaulted.id).status, "scheduled")

    # 8
    def test_meeting_room_uniqueness_is_unchanged(self):
        self.assertTrue(Appointment.__table__.c.meeting_room.unique)
        self._save(meeting_room="tok-room-a", discord_channel_attempt_token="77" * 32)
        db.session.add(self._appointment(
            topic="Clash", meeting_room="tok-room-a", discord_channel_attempt_token="88" * 32,
        ))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        # Several NULL meeting rooms remain allowed.
        self._save(topic="No room 1")
        self._save(topic="No room 2")

    # 9
    def test_channel_id_uniqueness_is_unchanged(self):
        indexes = {
            index["name"]: index for index in sa.inspect(db.engine).get_indexes("appointments")
        }
        self.assertIn(CHANNEL_INDEX, indexes)
        self.assertTrue(indexes[CHANNEL_INDEX]["unique"])
        self.assertEqual(indexes[CHANNEL_INDEX]["column_names"], ["discord_channel_id"])

        self._save(discord_channel_id="666666666666666666", discord_channel_attempt_token="99" * 32)
        db.session.add(self._appointment(
            topic="Second", discord_channel_id="666666666666666666",
            discord_channel_attempt_token="aa" * 32,
        ))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        # Several NULL channel IDs remain allowed.
        self._save(topic="No channel 1")
        self._save(topic="No channel 2")

    # 18
    def test_repeated_null_tokens_are_permitted_for_different_appointments(self):
        for index in range(3):
            self._save(topic="Null token %d" % index)
        null_count = Appointment.query.filter(
            Appointment.discord_channel_attempt_token.is_(None)
        ).count()
        self.assertEqual(null_count, 3)


class MigrationGraphTests(unittest.TestCase):
    """Reads the revision scripts only; no database is involved."""

    # 10
    def test_new_migration_depends_on_e5a1b9c3d4f8(self):
        script = _script_directory()
        revision = script.get_revision(NEW_REVISION)
        self.assertIsNotNone(revision)
        self.assertEqual(revision.down_revision, PARENT_REVISION)
        self.assertIsNone(revision.branch_labels or None)
        module = _load_new_migration_module()
        self.assertEqual(module.revision, NEW_REVISION)
        self.assertEqual(module.down_revision, PARENT_REVISION)

    # 11
    def test_migration_graph_has_one_linear_head(self):
        script = _script_directory()
        self.assertEqual(list(script.get_heads()), ["d0a1b2c3d4e5"])
        self.assertEqual(len(script.get_bases()), 1)

        revisions = list(script.walk_revisions())
        self.assertEqual(revisions[0].revision, "d0a1b2c3d4e5")
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

    # 12
    def test_total_migration_count_is_nine(self):
        script = _script_directory()
        revisions = list(script.walk_revisions())
        self.assertEqual(len(revisions), 10)
        self.assertEqual(tuple(r.revision for r in revisions), EXPECTED_CHAIN)
        files = [p for p in VERSIONS_DIR.glob("*.py") if p.name != "__init__.py"]
        self.assertEqual(len(files), 10)

    def test_migration_source_is_a_single_column_addition(self):
        source = _new_migration_path().read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(set(functions), {"upgrade", "downgrade"})
        self.assertEqual(source.count("batch_alter_table('appointments'"), 2)

        upgrade, downgrade = functions["upgrade"], functions["downgrade"]
        self.assertEqual(upgrade.count("add_column("), 1)
        self.assertEqual(downgrade.count("drop_column("), 1)
        for body in (upgrade, downgrade):
            self.assertIn(TOKEN_COLUMN, body)
            # Nothing but the token column is mentioned.
            self.assertNotIn("discord_channel", body.replace(TOKEN_COLUMN, ""))
            self.assertNotIn("meeting_room", body)
            for forbidden in (
                "create_index", "drop_index", "create_unique_constraint",
                "drop_constraint", "create_table", "drop_table", "alter_column",
                "execute(",
            ):
                self.assertNotIn(forbidden, body)
        self.assertNotIn("drop_column(", upgrade)
        self.assertNotIn("add_column(", downgrade)
        self.assertIn("nullable=True", upgrade)
        self.assertIn("length=64", upgrade)
        self.assertNotIn("server_default", upgrade)
        self.assertNotIn("unique", upgrade)


class AttemptTokenMigrationOnScratchDatabaseTests(unittest.TestCase):
    """Runs the new migration on a private in-memory database built here.

    The scratch ``appointments`` table imitates the schema at e5a1b9c3d4f8 and
    holds pre-existing rows. This never touches an existing database.
    """

    DATA_COLUMNS = (
        "id, provider_id, student_id, role, topic, start_time, duration_minutes, "
        "status, currency, meeting_room, discord_channel_id, discord_channel_created_at, "
        "discord_channel_status, discord_channel_last_attempt_at, created_at, updated_at"
    )

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
            "discord_channel_id VARCHAR(32), "
            "discord_channel_created_at DATETIME, "
            "discord_channel_status VARCHAR(20) DEFAULT 'unprovisioned' NOT NULL, "
            "discord_channel_last_attempt_at DATETIME, "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, "
            "PRIMARY KEY (id), "
            "CONSTRAINT uq_appointments_meeting_room UNIQUE (meeting_room))"
        )
        self.conn.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_appointments_discord_channel_id "
            "ON appointments (discord_channel_id)"
        )
        self._insert_row(
            1, room="room-1", channel="111111111111111111",
            created="2026-12-01 08:00:00.000000", status="provisioned",
            attempt="2026-12-01 07:59:00.000000",
        )
        self._insert_row(
            2, room="room-2", channel="222222222222222222",
            status="failed", attempt="2026-12-01 07:58:00.000000",
        )
        self._insert_row(3)  # no room, no channel, default channel status
        self.conn.commit()
        self.module = _load_new_migration_module()

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def _insert_row(self, row_id, room=None, channel=None, created=None, status=None, attempt=None):
        columns = [
            "id", "provider_id", "student_id", "role", "topic", "start_time",
            "duration_minutes", "status", "currency", "meeting_room",
            "discord_channel_id", "discord_channel_created_at",
            "discord_channel_last_attempt_at", "created_at", "updated_at",
        ]
        values = [
            ":id", "1", "2", "'mentor'", "'Legacy'", "'2026-12-01 09:00:00.000000'",
            "60", "'scheduled'", "'PHP'", ":room",
            ":channel", ":created", ":attempt",
            "'2026-11-01 00:00:00.000000'", "'2026-11-01 00:00:00.000000'",
        ]
        params = {
            "id": row_id, "room": room, "channel": channel,
            "created": created, "attempt": attempt,
        }
        if status is not None:
            columns.append("discord_channel_status")
            values.append(":dstatus")
            params["dstatus"] = status
        self.conn.execute(
            sa.text("INSERT INTO appointments (%s) VALUES (%s)" % (", ".join(columns), ", ".join(values))),
            params,
        )

    def _run(self, function):
        context = MigrationContext.configure(self.conn)
        with Operations.context(context):
            function()
        self.conn.commit()

    def _column_names(self):
        return {c["name"] for c in sa.inspect(self.conn).get_columns("appointments")}

    def _indexes(self):
        return {
            (i["name"], bool(i["unique"]), tuple(i["column_names"]))
            for i in sa.inspect(self.conn).get_indexes("appointments")
        }

    def _unique_constraints(self):
        return {
            (c["name"], tuple(c["column_names"]))
            for c in sa.inspect(self.conn).get_unique_constraints("appointments")
        }

    def _snapshot(self):
        rows = self.conn.execute(sa.text(
            "SELECT " + self.DATA_COLUMNS + " FROM appointments ORDER BY id"
        )).all()
        return [tuple(row) for row in rows]

    def _set_token(self, row_id, token):
        self.conn.execute(
            sa.text("UPDATE appointments SET discord_channel_attempt_token = :t WHERE id = :i"),
            {"t": token, "i": row_id},
        )
        self.conn.commit()

    def _assert_unique_violation(self, sql):
        with self.assertRaises(IntegrityError):
            self.conn.execute(sa.text(sql))
        self.conn.rollback()

    # 13
    def test_upgrade_adds_only_the_attempt_token_column(self):
        columns_before = self._column_names()
        indexes_before = self._indexes()
        uniques_before = self._unique_constraints()
        self.assertNotIn(TOKEN_COLUMN, columns_before)

        self._run(self.module.upgrade)

        columns_after = self._column_names()
        self.assertEqual(columns_after - columns_before, {TOKEN_COLUMN})
        self.assertTrue(columns_before <= columns_after)

        reflected = {
            c["name"]: c for c in sa.inspect(self.conn).get_columns("appointments")
        }[TOKEN_COLUMN]
        self.assertTrue(reflected["nullable"])
        self.assertEqual(reflected["type"].length, 64)
        self.assertIsNone(reflected["default"])

        # No index or unique constraint was added, changed or removed.
        self.assertEqual(self._indexes(), indexes_before)
        self.assertEqual(self._unique_constraints(), uniques_before)

    # 14
    def test_upgrade_preserves_existing_appointment_rows(self):
        before = self._snapshot()
        self.assertEqual(len(before), 3)

        self._run(self.module.upgrade)

        self.assertEqual(self._snapshot(), before)
        total = self.conn.execute(sa.text("SELECT COUNT(*) FROM appointments")).scalar_one()
        self.assertEqual(total, 3)
        null_tokens = self.conn.execute(sa.text(
            "SELECT COUNT(*) FROM appointments WHERE discord_channel_attempt_token IS NULL"
        )).scalar_one()
        self.assertEqual(null_tokens, 3)

    # 15
    def test_upgrade_preserves_existing_discord_channel_data(self):
        self._run(self.module.upgrade)

        rows = self.conn.execute(sa.text(
            "SELECT id, discord_channel_id, discord_channel_created_at, "
            "discord_channel_status, discord_channel_last_attempt_at "
            "FROM appointments ORDER BY id"
        )).all()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (1, "111111111111111111", "2026-12-01 08:00:00.000000",
                 "provisioned", "2026-12-01 07:59:00.000000"),
                (2, "222222222222222222", None,
                 "failed", "2026-12-01 07:58:00.000000"),
                (3, None, None, "unprovisioned", None),
            ],
        )

        # The channel ID unique index still works, and the token is writable.
        self.assertIn((CHANNEL_INDEX, True, ("discord_channel_id",)), self._indexes())
        self._assert_unique_violation(
            "UPDATE appointments SET discord_channel_id = '111111111111111111' WHERE id = 3"
        )
        self._set_token(1, "bb" * 32)
        stored = self.conn.execute(sa.text(
            "SELECT discord_channel_attempt_token FROM appointments WHERE id = 1"
        )).scalar_one()
        self.assertEqual(stored, "bb" * 32)

    # 16
    def test_downgrade_removes_only_the_attempt_token_column(self):
        columns_before = self._column_names()
        data_before = self._snapshot()

        self._run(self.module.upgrade)
        self._set_token(1, "cc" * 32)
        self._set_token(2, "dd" * 32)
        self._run(self.module.downgrade)

        self.assertEqual(self._column_names(), columns_before)
        self.assertNotIn(TOKEN_COLUMN, self._column_names())
        self.assertEqual(self._snapshot(), data_before)

    # 17
    def test_downgrade_preserves_channel_indexes_and_meeting_room_uniqueness(self):
        indexes_before = self._indexes()
        uniques_before = self._unique_constraints()
        self.assertIn((CHANNEL_INDEX, True, ("discord_channel_id",)), indexes_before)
        self.assertIn((MEETING_ROOM_CONSTRAINT, ("meeting_room",)), uniques_before)

        self._run(self.module.upgrade)
        self._run(self.module.downgrade)

        # The table was rebuilt by the batch operation; everything else survives.
        self.assertEqual(self._indexes(), indexes_before)
        self.assertEqual(self._unique_constraints(), uniques_before)
        self._assert_unique_violation(
            "UPDATE appointments SET meeting_room = 'room-1' WHERE id = 2"
        )
        self._assert_unique_violation(
            "UPDATE appointments SET discord_channel_id = '111111111111111111' WHERE id = 3"
        )

        # The channel status server default survives the rebuild.
        self._insert_row(4)
        self.conn.commit()
        status = self.conn.execute(sa.text(
            "SELECT discord_channel_status FROM appointments WHERE id = 4"
        )).scalar_one()
        self.assertEqual(status, "unprovisioned")

    # 18
    def test_repeated_null_tokens_are_permitted_after_upgrade(self):
        self._run(self.module.upgrade)
        self._insert_row(4)
        self._insert_row(5, room="room-5")
        self.conn.commit()
        null_tokens = self.conn.execute(sa.text(
            "SELECT COUNT(*) FROM appointments WHERE discord_channel_attempt_token IS NULL"
        )).scalar_one()
        self.assertEqual(null_tokens, 5)


if __name__ == "__main__":
    unittest.main()
