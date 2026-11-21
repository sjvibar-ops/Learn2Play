from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from psycopg.conninfo import conninfo_to_dict, make_conninfo


ROOT = Path(__file__).resolve().parents[1]
backup = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts" / "supabase_development_backup.py")))
Blocked = backup.catalog.Blocked
PASSWORD = "offline-backup-fixture"


class DevelopmentBackupTests(unittest.TestCase):
    def setUp(self):
        self.host = backup.development.DIRECT_HOST
        self.args = SimpleNamespace(expected_host=self.host, expected_port=5432, expected_runtime_role="fixture")
        self.identity = dict(database="postgres", schema="public", role="fixture",
                             session_role="fixture", read_only="on", version="170011")

    def test_rejects_production_unexpected_host_and_wrong_port(self):
        for host, port in ((f"db.{backup.catalog.PRODUCTION}.supabase.co", 5432),
                           ("example.invalid", 5432), (self.host, 9999)):
            with self.subTest(host=host, port=port), self.assertRaises(Blocked):
                backup.development.connection_parameters(
                    f"postgresql://fixture:{PASSWORD}@{host}:{port}/postgres", self.args, conninfo_to_dict,
                )

    def test_missing_tools_and_existing_destination(self):
        with TemporaryDirectory() as root:
            home = Path(root)
            with self.assertRaises(Blocked):
                backup.tools(home)
            parent = home / "AppData" / "Local"
            parent.mkdir(parents=True)
            existing = parent / "Learn2Play-Development-fixture"
            existing.mkdir()
            with self.assertRaises(Blocked):
                backup.destination_for(home, ROOT, "fixture")

    def test_missing_or_invalid_confirmation_stops(self):
        for response in ("", "yes", "BACK UP PRODUCTION"):
            with self.subTest(response=response), self.assertRaises(Blocked):
                backup.require_confirmation(response)

    def test_wrong_database_or_unreadable_scope_stops(self):
        connection = Mock()
        with self.assertRaises(Blocked):
            backup.verify_scope(connection, dict(self.identity, database="other"), self.args)
        connection.execute.assert_not_called()
        connection.execute.return_value.fetchone.return_value = {"allowed": True}
        rows = [dict(name=name, kind="r", readable=True, rls_active=False,
                     inheritance=False, extension_member=False) for name in backup.TABLES]
        rows.extend(dict(name=name, kind="S", readable=True, rls_active=False,
                         inheritance=False, extension_member=False) for name in backup.BASELINE_SEQUENCES)
        connection.execute.return_value.fetchall.return_value = rows
        for override in ({"readable": False}, {"rls_active": True}, {"extension_member": True}):
            with self.subTest(override=override), patch.dict(rows[0], override), self.assertRaises(Blocked):
                backup.verify_scope(connection, self.identity, self.args)

    def test_tool_failure_and_warning_are_redacted(self):
        for result in (SimpleNamespace(returncode=1, stderr=b"", stdout=PASSWORD.encode()),
                       SimpleNamespace(returncode=0, stderr=PASSWORD.encode(), stdout=b"")):
            with patch.object(backup.subprocess, "run", return_value=result), self.assertRaises(Blocked) as raised:
                backup.run_tool(Path("pg_dump.exe"), [])
            self.assertNotIn(PASSWORD, str(raised.exception))

    def test_dump_or_listing_failure_does_not_finalize(self):
        dump, restore = Path("pg_dump.exe"), Path("pg_restore.exe")
        parameters = dict(host=self.host, user="fixture", password=PASSWORD, dbname="postgres", sslmode="verify-full")
        for failure in (dump, restore):
            with self.subTest(failure=failure):
                connection = Mock()
                connection.pgconn.ssl_in_use = True
                connection.execute.return_value.fetchone.return_value = {"snapshot": "00000001-00000002-1"}
                connect = MagicMock()
                connect.return_value.__enter__.return_value = connection
                finalize = Mock()

                def run(tool, arguments, env=None):
                    self.assertNotIn(PASSWORD, str(arguments))
                    if tool == dump:
                        self.assertEqual(env["PGPASSWORD"], PASSWORD)
                        self.assertIn("--snapshot=00000001-00000002-1", arguments)
                    if tool == failure:
                        raise Blocked("offline command failure")
                    return b""

                with patch.dict(backup.create_archive.__globals__, {
                    "run_tool": run, "create_restricted_directory": Mock(),
                    "verify_scope": Mock(return_value=set()), "write_manifest": finalize,
                }), self.assertRaises(Blocked):
                    backup.create_archive(Path("unused"), (dump, restore), parameters, self.args,
                                          connect, None, make_conninfo)
                finalize.assert_not_called()
                self.assertEqual(connection.execute.call_args.args, ("ROLLBACK",))

    def test_incomplete_listing_stops(self):
        listing = b"".join(f"1; 0 1 TABLE DATA public {name} owner\n".encode() for name in backup.TABLES)
        backup.verify_listing(listing, set())
        with self.assertRaises(Blocked):
            backup.verify_listing(listing, {"expected_id_seq"})
        with self.assertRaises(Blocked):
            backup.verify_listing(b"", set())

    def test_checksum_and_safe_manifest_with_fake_archive(self):
        with TemporaryDirectory() as root:
            destination = Path(root)
            archive = destination / "development.dump.partial"
            archive.write_bytes(b"PGDMP offline fixture, not an actual backup")
            with self.assertRaises(Blocked):
                backup.write_manifest(destination, archive, "")
            self.assertFalse((destination / "manifest.json").exists())
            checksum = backup.archive_checksum(archive)
            backup.write_manifest(destination, archive, checksum)
            manifest = (destination / "manifest.json").read_text(encoding="utf-8")
            result = json.loads(manifest)
            self.assertEqual(result["sha256"], checksum)
            self.assertEqual(result["status"], "ARCHIVE CREATED AND LISTED")
            self.assertFalse(result["restore_verified"])
            self.assertNotIn(PASSWORD, manifest)
            self.assertFalse(archive.exists())

    def test_cli_redacts_unexpected_error(self):
        with patch.dict(backup.cli.__globals__, {"main": Mock(side_effect=RuntimeError(PASSWORD))}), redirect_stdout(
            StringIO()
        ) as output:
            self.assertEqual(backup.cli(), 1)
        self.assertNotIn(PASSWORD, output.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows backup directory")
    def test_restricted_directory_is_writable_and_created_exclusively(self):
        with TemporaryDirectory() as root:
            destination = Path(root) / "acl-fixture"
            backup.create_restricted_directory(destination)
            (destination / "fixture.txt").write_text("offline", encoding="utf-8")
            with self.assertRaises(Blocked):
                backup.create_restricted_directory(destination)
