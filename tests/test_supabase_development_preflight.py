from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from psycopg.conninfo import conninfo_to_dict


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "supabase_development_preflight.py"
diagnostic = SimpleNamespace(**runpy.run_path(str(SCRIPT)))
catalog = diagnostic.catalog
DEMO_PASSWORD = "offline-synthetic-fixture"
POOLER = "aws-0-ap-northeast-2.pooler.supabase.com"


class DevelopmentPreflightTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            expected_host=diagnostic.DIRECT_HOST, expected_port=5432,
            expected_runtime_role="fixture_role", receipt_bucket="payment-receipts",
        )

    def parse(self, host, username="fixture_role", password=DEMO_PASSWORD, database="postgres", query=""):
        url = f"postgresql://{username}:{password}@{host}/{database}{query}"
        return diagnostic.connection_parameters(url, self.args, conninfo_to_dict)

    def test_confirmed_development_direct_and_pooler_require_verified_tls(self):
        self.assertEqual(self.parse(diagnostic.DIRECT_HOST)["sslmode"], "verify-full")
        self.args.expected_host = POOLER
        result = self.parse(POOLER, "fixture_role." + catalog.DEVELOPMENT)
        self.assertEqual(result["host"], POOLER)
        self.assertEqual(result["gssencmode"], "disable")

    def test_wrong_project_and_production_are_rejected(self):
        self.args.expected_host = POOLER
        for project in ("differentprojectref", catalog.PRODUCTION):
            with self.subTest(project=project), self.assertRaises(catalog.Blocked):
                self.parse(POOLER, "fixture_role." + project)
        with self.assertRaisesRegex(catalog.Blocked, "Production project rejected"):
            self.parse(f"db.{catalog.PRODUCTION}.supabase.co")

    def test_missing_configuration_and_unexpected_routing_are_rejected(self):
        for changes in (
            dict(password=""), dict(database=""), dict(database="other"),
            dict(host="other.example.test"), dict(query="?hostaddr=192.0.2.1"),
            dict(query="?sslmode=require"), dict(host=diagnostic.DIRECT_HOST + ":6543"),
        ):
            parameters = {"host": diagnostic.DIRECT_HOST, **changes}
            with self.subTest(changes=changes), self.assertRaises(catalog.Blocked):
                self.parse(**parameters)
        with patch("sys.argv", [str(SCRIPT)]), redirect_stdout(StringIO()) as output:
            self.assertEqual(diagnostic.cli(), 2)
        self.assertIn("BLOCKER", output.getvalue())

    def test_unverified_tls_stops_before_sql(self):
        connection = Mock()
        connection.pgconn.ssl_in_use = False
        with self.assertRaises(catalog.Blocked):
            diagnostic.inspect_target(connection, self.args, (), {}, {}, RuntimeError)
        connection.execute.assert_not_called()

    def test_read_only_failure_rolls_back_without_inspection(self):
        connection = Mock()
        connection.pgconn.ssl_in_use = True
        connection.execute.return_value.fetchone.return_value = {"read_only": "off"}
        with patch.object(catalog, "inspect") as inspect, self.assertRaises(catalog.Blocked):
            diagnostic.inspect_target(connection, self.args, (), {}, {}, RuntimeError)
        inspect.assert_not_called()
        self.assertEqual(connection.execute.call_args_list, [
            call("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"), call(catalog.IDENTITY), call("ROLLBACK"),
        ])

    def test_storage_failure_is_reported_separately_and_rolls_back(self):
        connection = Mock()
        connection.pgconn.ssl_in_use = True
        connection.execute.return_value.fetchone.return_value = {
            "read_only": "on", "database": "postgres", "schema": "public",
            "role": "fixture_role", "session_role": "fixture_role",
        }
        connection.execute.return_value.fetchall.return_value = [
            {"signature": "fixture", "present": True, "result_matches": True, "executable": True}
        ]
        database = diagnostic.section(policies=[])
        storage = diagnostic.section()
        with patch.object(catalog, "inspect", return_value=({}, {})), patch.object(
            catalog, "inspect_storage", side_effect=RuntimeError(DEMO_PASSWORD),
        ):
            diagnostic.inspect_target(connection, self.args, (), database, storage, RuntimeError)
        self.assertEqual(diagnostic.summarize(database), "PASS")
        self.assertEqual(diagnostic.summarize(storage), "NOT VERIFIED")
        self.assertNotIn(DEMO_PASSWORD, str(storage))
        self.assertEqual(connection.execute.call_args, call("ROLLBACK"))

    def test_parser_and_cli_suppress_secret_in_unexpected_exception(self):
        parser = Mock(side_effect=ValueError(DEMO_PASSWORD))
        with self.assertRaises(catalog.Blocked) as error:
            diagnostic.connection_parameters(
                f"postgresql://fixture_role:{DEMO_PASSWORD}@{diagnostic.DIRECT_HOST}/postgres",
                self.args, parser,
            )
        self.assertNotIn(DEMO_PASSWORD, str(error.exception))
        with patch.dict(diagnostic.cli.__globals__, {"main": Mock(side_effect=RuntimeError(DEMO_PASSWORD))}):
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(diagnostic.cli(), 2)
        self.assertIn("BLOCKER", output.getvalue())
        self.assertNotIn(DEMO_PASSWORD, output.getvalue())

    def test_catalog_inventory_matches_reviewed_head(self):
        self.assertEqual(catalog.migration_graph()[0], diagnostic.EXPECTED_HEAD)
        tables, _, _ = catalog.local_schema()
        self.assertEqual(len(tables), 21)

    def test_readable_bucket_inventory_reports_missing_required_bucket(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = [
            {"name": "avatars", "public": True, "file_size_limit": 2 * 1024**2,
             "allowed_mime_types": ["image/jpeg", "image/png", "image/webp"]},
            {"name": "credentials", "public": False, "file_size_limit": 5 * 1024**2,
             "allowed_mime_types": ["application/pdf", "image/jpeg", "image/png"]},
        ]
        key = ("storage", "buckets")
        relations = {key: {"kind": "r", "readable": True, "rls_active": False}}
        columns = {key: {
            name: {"type_schema": "pg_catalog", "internal_type": internal_type, "generated": False}
            for name, internal_type in (
                ("name", "text"), ("public", "bool"), ("file_size_limit", "int8"),
                ("allowed_mime_types", "_text"),
            )
        }}
        storage = diagnostic.section()
        catalog.inspect_storage(connection, self.args.receipt_bucket, relations, columns, storage)
        self.assertEqual(diagnostic.summarize(storage), "BLOCKER")
        self.assertEqual(storage["problems"], ["Storage bucket missing or privacy differs: payment-receipts."])
