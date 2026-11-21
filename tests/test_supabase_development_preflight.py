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
            diagnostic.inspect_target(connection, self.args, (), {}, {}, {}, {}, RuntimeError)
        connection.execute.assert_not_called()

    def test_read_only_failure_rolls_back_without_inspection(self):
        connection = Mock()
        connection.pgconn.ssl_in_use = True
        connection.execute.return_value.fetchone.return_value = {"read_only": "off"}
        with patch.object(catalog, "inspect") as inspect, self.assertRaises(catalog.Blocked):
            diagnostic.inspect_target(connection, self.args, (), {}, {}, {}, {}, RuntimeError)
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
        financial = diagnostic.section()
        access = diagnostic.section()
        with patch.object(catalog, "inspect", return_value=({}, {})), patch.object(
            catalog, "inspect_storage", side_effect=RuntimeError(DEMO_PASSWORD),
        ), patch.dict(diagnostic.inspect_target.__globals__, {
            "check_definitions": Mock(), "check_trigger_definitions": Mock(),
            "check_financial_runtime": Mock(),
        }):
            diagnostic.inspect_target(connection, self.args, (None,) * 5, database, financial, storage, access, RuntimeError)
        self.assertEqual(diagnostic.summarize(database), "PASS")
        self.assertEqual(diagnostic.summarize(storage), "NOT VERIFIED")
        self.assertNotIn(DEMO_PASSWORD, str(storage))
        self.assertIsNone(access["objects_relation"])
        self.assertEqual(diagnostic.summarize(access), "NOT VERIFIED")
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
        connection.execute.return_value.fetchall.return_value.append({
            "name": "payment-receipts", "public": False, "file_size_limit": 5 * 1024**2,
            "allowed_mime_types": ["image/png", "image/jpeg"],
        })
        storage = diagnostic.section()
        catalog.inspect_storage(connection, self.args.receipt_bucket, relations, columns, storage)
        self.assertEqual(diagnostic.summarize(storage), "PASS")

    def test_partial_predicates_and_check_constraints(self):
        for expected, matching, changed in (
            ("status = 'VERIFIED'", "((status)::text = 'VERIFIED'::text)", "status = 'PENDING'"),
            ("status = 'pending'", "((status)::text = 'pending'::text)", "status = 'accepted'"),
            ("points > 0", "(points > 0)", "points >= 0"),
        ):
            with self.subTest(expected=expected):
                report = diagnostic.section()
                diagnostic.compare_expression(expected, matching, "fixture", report)
                self.assertEqual(diagnostic.summarize(report), "PASS")
                diagnostic.compare_expression(expected, changed, "fixture", report)
                self.assertEqual(diagnostic.summarize(report), "NOT VERIFIED")

    def test_changed_trigger_condition_and_columns(self):
        connection = Mock()
        trigger = {"table_name": "ledger_transactions", "name": "phase2_check_allocation"}
        prefix = "CREATE TRIGGER phase2_check_allocation AFTER UPDATE OF {} ON public.ledger_transactions"
        suffix = " FOR EACH ROW WHEN ({}) EXECUTE FUNCTION public.phase2_check_allocation()"
        for column, expression, status in (
            ("sealed", "NEW.sealed AND NEW.kind = 'allocation'", "PASS"),
            ("sealed", "NEW.sealed OR NEW.kind = 'allocation'", "NOT VERIFIED"),
            ("kind", "NEW.sealed AND NEW.kind = 'allocation'", "BLOCKER"),
        ):
            with self.subTest(column=column, expression=expression):
                connection.execute.return_value.fetchall.return_value = [
                    dict(trigger, definition=prefix.format(column) + suffix.format(expression))
                ]
                report = diagnostic.section()
                diagnostic.check_trigger_definitions(connection, report)
                self.assertEqual(diagnostic.summarize(report), status)

    def test_financial_runtime_settings_and_restrictive_rls(self):
        runtime = dict(replication_role="origin", default_isolation="read committed",
                       schema_usage=True, update_sealed=True, rls_active=True)
        policies = [dict(command="*", permissive=True, using_expression="true", check_expression="true")]
        for changes, status in (
            ({}, "PASS"), ({"update_sealed": False}, "BLOCKER"),
            ({"replication_role": "replica"}, "BLOCKER"), ({"default_isolation": "repeatable read"}, "BLOCKER"),
        ):
            with self.subTest(changes=changes):
                report = diagnostic.section()
                diagnostic.check_financial_runtime({**runtime, **changes}, policies, report)
                self.assertEqual(diagnostic.summarize(report), status)
        policies.append(dict(command="w", permissive=False, using_expression="false", check_expression=None))
        report = diagnostic.section()
        diagnostic.check_financial_runtime(runtime, policies, report)
        self.assertEqual(diagnostic.summarize(report), "NOT VERIFIED")

    def test_matching_buckets_cannot_override_unverified_access(self):
        metadata = diagnostic.section()
        access = diagnostic.section()
        access["unverified"].append("Storage API not exercised")
        self.assertEqual(diagnostic.aggregate(diagnostic.section(), metadata, access), "NOT VERIFIED")
        self.assertEqual(metadata["status"], "PASS")
        self.assertEqual(access["status"], "NOT VERIFIED")
        financial = diagnostic.section()
        financial["problems"].append("fixture blocker")
        self.assertEqual(diagnostic.aggregate(financial, metadata, access), "BLOCKER")

    def test_financial_query_error_is_redacted_and_rolls_back(self):
        connection = Mock()
        connection.pgconn.ssl_in_use = True
        connection.execute.return_value.fetchone.return_value = {
            "read_only": "on", "database": "postgres", "schema": "public",
            "role": "fixture_role", "session_role": "fixture_role",
        }
        connection.execute.return_value.fetchall.side_effect = RuntimeError(DEMO_PASSWORD)
        database, financial, metadata, access = (diagnostic.section() for _ in range(4))
        with patch.object(catalog, "inspect", return_value=({}, {})), patch.dict(
            diagnostic.inspect_target.__globals__, {"check_definitions": Mock()},
        ):
            diagnostic.inspect_target(connection, self.args, (None,) * 5,
                                      database, financial, metadata, access, RuntimeError)
        self.assertEqual(diagnostic.summarize(financial), "NOT VERIFIED")
        self.assertEqual(diagnostic.summarize(metadata), "NOT VERIFIED")
        self.assertNotIn(DEMO_PASSWORD, str(financial))
        self.assertEqual(connection.execute.call_args, call("ROLLBACK"))
