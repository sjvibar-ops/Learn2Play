from contextlib import nullcontext
from unittest.mock import Mock

import psycopg
import pytest

from scripts import diagnose_supabase_render_connection as diagnostic


URL = "postgresql://postgres.pxiwoowpoewxvnsjwfdw:secret-example@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres?sslmode=require"


def test_connection_error_never_reports_supplied_secret(monkeypatch, capsys):
    def fail(**kwargs):
        assert kwargs["sslmode"] == "verify-full"
        assert "default_transaction_read_only=on" in kwargs["options"]
        raise psycopg.OperationalError(URL)
    monkeypatch.setattr(diagnostic.psycopg, "connect", fail)
    assert diagnostic.main({"DATABASE_URL": URL}) == 1
    captured = capsys.readouterr()
    assert "Diagnostic stopped" in captured.err
    assert URL not in captured.err + captured.out
    assert "secret-example" not in captured.err + captured.out


def test_missing_url_uses_secure_prompt_and_stops_on_empty(monkeypatch):
    prompted = []
    monkeypatch.setattr(diagnostic, "read_development_url", lambda: prompted.append(True) or "")
    monkeypatch.setattr(diagnostic.psycopg, "connect", lambda **_: pytest.fail("unexpected connection"))
    assert diagnostic.main({}) == 1
    assert prompted == [True]


@pytest.mark.parametrize("url", [
    URL.replace(":5432", ":6543"),
    URL.replace(".pooler.supabase.com", ".example.com"),
    URL.replace(diagnostic.PROJECT, "wrongproject"),
    URL + "&hostaddr=127.0.0.1",
])
def test_rejects_unapproved_targets(url):
    with pytest.raises(diagnostic.Blocked):
        diagnostic.parameters(url)


class Connection:
    read_only = False
    isolation_level = None
    rolled_back = False

    def transaction(self):
        return nullcontext()

    def rollback(self):
        self.rolled_back = True

    def execute(self, sql, args):
        assert sql.lstrip().startswith("SELECT ")
        assert self.read_only is True
        assert self.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
        class Result:
            def fetchall(self):
                if sql == diagnostic.IDENTITY:
                    return [{"read_only": "on", "isolation": "repeatable read",
                             "database_name": "postgres", "catalog_path_verified": True}]
                if "to_regclass" in sql:
                    return [{"present": True}]
                if "SELECT version_num" in sql:
                    return [{"version_num": diagnostic.EXPECTED_REVISION}]
                if sql == diagnostic.TABLES:
                    raise psycopg.errors.InsufficientPrivilege("private diagnostic")
                return []
        return Result()


def test_catalog_only_and_transaction_rollback():
    connection = Connection()
    report = diagnostic.inspect(connection)
    assert connection.rolled_back
    assert report["catalog"]["tables"] == {
        "status": "unavailable", "reason": "PostgreSQL permission or catalog access denied.",
    }
    assert report["baseline"]["alembic"]["matches_expected"]
    assert report["financial_runtime_exists"] is False


def test_success_output_has_no_connection_arguments(monkeypatch, capsys):
    monkeypatch.setattr(diagnostic.psycopg, "connect", lambda **_: nullcontext(Connection()))
    assert diagnostic.main({"DATABASE_URL": URL}) == 2
    captured = capsys.readouterr()
    assert URL not in captured.out + captured.err
    assert "secret-example" not in captured.out + captured.err
    assert "repeatable read" in captured.out


@pytest.mark.parametrize(("url", "category"), [
    ("bogus://secret-example", "Invalid connection URL"),
    ("postgresql://[invalid:secret-example", "Invalid connection URL"),
    (URL.replace("aws-0", "aws-1"), "Unexpected host"),
    (URL.replace(":5432", ":6543"), "Unexpected port"),
    (URL.replace("/postgres?", "/other?"), "Unexpected database"),
    (URL.replace(diagnostic.PROJECT, "wrongproject"), "Unexpected username"),
    (URL + "&hostaddr=127.0.0.1", "Unexpected connection URL options"),
    (URL.replace("sslmode=require", "sslmode=disable"), "Unsupported TLS mode"),
])
def test_reports_target_rejection_without_connecting(url, category, monkeypatch, capsys):
    monkeypatch.setattr(diagnostic.psycopg, "connect", lambda **_: pytest.fail("unexpected connection"))
    assert diagnostic.main({"DATABASE_URL": url}) == 1
    output = capsys.readouterr().err
    assert category in output
    assert url not in output and "secret-example" not in output


@pytest.mark.parametrize(("failure_type", "message", "category"), [
    (psycopg.OperationalError, "certificate verify failed", "TLS certificate or hostname verification failed (detail unavailable)"),
    (psycopg.OperationalError, "could not load system root certificate paths", "OpenSSL could not load its default CA trust paths"),
    (psycopg.OperationalError, "root certificate file does not exist", "TLS root certificate file is missing"),
    (psycopg.OperationalError, "certificate has expired", "TLS certificate in the chain has expired"),
    (psycopg.OperationalError, "certificate does not match host name", "TLS hostname verification failed"),
    (psycopg.OperationalError, "unable to get local issuer certificate", "could not be linked to a trusted CA"),
    (psycopg.OperationalError, "invalid CA certificate", "TLS certificate chain validation failed"),
    (psycopg.OperationalError, "could not translate host name", "DNS resolution failed"),
    (psycopg.errors.ConnectionTimeout, "", "Database connection timed out"),
    (psycopg.errors.InvalidPassword, "", "Database authentication failed"),
    (psycopg.OperationalError, "password authentication failed", "Database authentication failed"),
    (psycopg.errors.InsufficientPrivilege, "", "PostgreSQL permission or catalog access denied"),
    (psycopg.errors.ReadOnlySqlTransaction, "", "Read-only transaction setup or verification failed"),
    (psycopg.errors.QueryCanceled, "", "PostgreSQL query was canceled or timed out"),
    (psycopg.errors.LockNotAvailable, "", "PostgreSQL lock could not be acquired"),
    (psycopg.OperationalError, "connection refused", "Database connection refused"),
    (psycopg.OperationalError, "unsupported startup parameter: options", "The pooler rejected a startup parameter"),
    (psycopg.OperationalError, "unrecognized error", "Unexpected connection error (unclassified)"),
])
def test_driver_failures_use_fixed_categories(failure_type, message, category, monkeypatch, capsys):
    def fail(**kwargs):
        assert kwargs["sslmode"] == "verify-full"
        assert kwargs["sslrootcert"] == "system"
        raise failure_type(f"{message} {URL}")

    monkeypatch.setattr(diagnostic.psycopg, "connect", fail)
    assert diagnostic.main({"DATABASE_URL": URL}) == 1
    output = capsys.readouterr().err
    assert category in output
    assert URL not in output and "secret-example" not in output


def test_wrong_transaction_state_stops_before_catalog_access(monkeypatch, capsys):
    connection = Connection()

    def fake_rows(_, statement, args=()):
        assert statement == diagnostic.IDENTITY
        return [{"read_only": "off", "isolation": "repeatable read", "database_name": "postgres", "catalog_path_verified": True}]

    monkeypatch.setattr(diagnostic, "rows", fake_rows)
    monkeypatch.setattr(diagnostic.psycopg, "connect", lambda **_: nullcontext(connection))
    assert diagnostic.main({"DATABASE_URL": URL}) == 1
    assert connection.rolled_back
    assert "Read-only transaction or database identity verification failed" in capsys.readouterr().err


def test_console_and_environment_errors_are_sanitized(monkeypatch, capsys):
    def fail():
        raise OSError(URL)

    monkeypatch.setattr(diagnostic, "read_development_url", fail)
    assert diagnostic.main({}) == 1
    assert diagnostic.main({"PGHOSTADDR": "secret-example"}) == 1
    output = capsys.readouterr().err
    assert "Secure console input failed" in output
    assert "Conflicting PostgreSQL environment configuration" in output
    assert URL not in output and "secret-example" not in output


def test_explicit_ca_passed_to_libpq_without_changing_hostname_verification(tmp_path, monkeypatch, capsys):
    path = tmp_path / "development-ca.pem"
    path.touch()
    context = Mock()
    context.cert_store_stats.return_value = {"x509_ca": 1}
    monkeypatch.setattr(diagnostic.ssl, "SSLContext", lambda _: context)
    connect = Mock(return_value=nullcontext(Connection()))
    monkeypatch.setattr(diagnostic.psycopg, "connect", connect)
    assert diagnostic.main({"DATABASE_URL": URL, diagnostic.CA_FILE_ENV: str(path)}) == 2
    context.load_verify_locations.assert_called_once_with(cafile=str(path))
    connect.assert_called_once()
    options = connect.call_args.kwargs
    assert options["sslrootcert"] == str(path)
    assert options["sslmode"] == "verify-full"
    assert options["host"] == "aws-0-ap-northeast-2.pooler.supabase.com"
    output = capsys.readouterr()
    assert all(value not in output.out + output.err for value in (URL, "secret-example", str(path)))


@pytest.mark.parametrize(("case", "category"), [
    ("missing", "Development CA file is missing"),
    ("unreadable", "Development CA file could not be read"),
    ("malformed", "Invalid Development CA file"),
    ("relative", "Invalid Development CA configuration"),
    ("directory", "Invalid Development CA configuration"),
    ("empty", "Invalid Development CA configuration"),
])
def test_invalid_ca_fails_closed(tmp_path, monkeypatch, capsys, case, category):
    path = tmp_path / "secret-example.pem"
    value = str(path)
    if case in {"unreadable", "malformed"}:
        path.write_text("invalid certificate")
    if case == "unreadable":
        context = Mock()
        context.load_verify_locations.side_effect = PermissionError(value)
        monkeypatch.setattr(diagnostic.ssl, "SSLContext", lambda _: context)
    if case in {"relative", "directory", "empty"}:
        value = {"relative": "development-ca.pem", "directory": str(tmp_path), "empty": ""}[case]
    connect = Mock()
    monkeypatch.setattr(diagnostic.psycopg, "connect", connect)
    assert diagnostic.main({"DATABASE_URL": URL, diagnostic.CA_FILE_ENV: value}) == 1
    connect.assert_not_called()
    output = capsys.readouterr()
    assert category in output.err
    assert all(secret not in output.out + output.err for secret in (URL, "secret-example", str(path)))


@pytest.mark.parametrize("env_var", ["SSL_CERT_FILE", "SSL_CERT_DIR", "PGSSLROOTCERT"])
def test_ambient_trust_override_fails_closed(env_var, monkeypatch, capsys):
    connect = Mock()
    monkeypatch.setattr(diagnostic.psycopg, "connect", connect)
    assert diagnostic.main({"DATABASE_URL": URL, env_var: "secret-example"}) == 1
    connect.assert_not_called()
    output = capsys.readouterr()
    assert "Diagnostic stopped" in output.err
    assert "secret-example" not in output.out + output.err


@pytest.fixture
def baseline_fixture(monkeypatch):
    expected = diagnostic.preflight
    principals = set(expected.PRINCIPALS)
    all_principals = principals | {"PUBLIC"}
    sequence_names = {f"{t}_id_seq" for t in expected.ID_TABLES}
    catalog = {name: [] for name in diagnostic.CHECKS}
    catalog["tables"] = [dict(table_name=t, owner="postgres", rls_enabled=False,
                              rls_forced=False, relkind="r") for t in expected.BASE_TABLES]
    catalog["roles"] = [dict(rolname=p, rolbypassrls=p=="postgres", rolcanlogin=p=="postgres")
                        for p in principals]
    catalog["sequences"] = [dict(rolname=p, relname=t, owner="postgres")
                            for p in principals for t in sequence_names]
    catalog["table_privileges"] = [dict(rolname=p, relname=t)
                                   for p in principals for t in expected.BASE_TABLES]
    catalog["public_table_privileges"] = [dict(relation=t) for t in expected.BASE_TABLES]
    catalog["schema_privileges"] = [dict(principal=p, can_use=True, can_create=p=="postgres")
                                    for p in all_principals]
    catalog["authority"] = [dict(relation=t, relkind="S" if t in sequence_names else "r",
                                 owner_authority=True, can_reference=True, can_trigger=True)
                             for t in sequence_names | expected.BASE_TABLES]
    catalog["columns"] = [
        dict(table_name="appointments", name=name, position=i, type=kind, not_null=required,
             default_expression="nextval('public.appointments_id_seq'::regclass)" if name=="id" else None,
             identity_kind="", generated_kind="")
        for i, (name, kind, required) in enumerate(expected.APPOINTMENT, 1)
    ] + [dict(table_name="users", name="leaderboard_opt_in", type="boolean",
              not_null=True, default_expression="false")] + [
        dict(table_name=t, name="fixture_column", default_expression=None)
        for t in expected.BASE_TABLES - {"users", "appointments"}
    ]
    catalog["browser_columns"] = [
        dict(principal=p, relation=row["table_name"], relkind="r", column_name=row["name"],
             can_read=False, can_insert=False, can_update=False)
        for p in all_principals for row in catalog["columns"]
    ]
    catalog["constraints"] = [
        dict(table_name=t, kind=kind, columns=cols, target=f"public.{target}" if target else None,
             target_columns=refs, valid=True, deferrable=False, deferred=False, expression=None,
             on_update="a", on_delete="a", match_type="s",
             name="uq_appointments_meeting_room" if cols==("meeting_room",) else "constraint")
        for t, kind, cols, target, refs in expected.CONSTRAINTS
    ] + [
        dict(table_name="xp_events", kind="c", target=None, target_columns=[], columns=[],
             valid=True, deferrable=False, deferred=False, name=name, expression=expression)
        for name, expression in {
            "ck_xp_events_path": "(path::text = ANY (ARRAY['academic'::character varying, 'gaming'::character varying]::text[]))",
            "ck_xp_events_positive_points": "(points > 0)",
        }.items()
    ]
    catalog["indexes"] = [dict(table_name=t, columns=cols, unique=unique, name="index",
                              valid=True, ready=True, live=True, immediate=True, method="btree",
                              partial=False, nulls_not_distinct=False)
                          for t, cols, unique in expected.INDEXES]
    catalog["sessions"] = [dict(state_unavailable=0, idle_in_transaction=0, visibly_blocked=0,
                               active=1, oldest_visible_transaction_seconds=3)]
    revisions = [{"version_num": diagnostic.EXPECTED_REVISION}]
    connection = Connection()

    def read(conn, statement, args=()):
        assert conn is connection
        assert statement.lstrip().startswith("SELECT ")
        assert conn.read_only and conn.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
        if statement == diagnostic.IDENTITY:
            return [dict(read_only="on", isolation="repeatable read", database_name="postgres",
                         effective_role="postgres", session_role="postgres", catalog_path_verified=True)]
        if "to_regclass" in statement:
            return [{"present": True}]
        if "SELECT version_num" in statement:
            return revisions
        if statement == "SELECT count(*) AS payment_count FROM public.payments":
            return [{"payment_count": 0}]
        section = next(name for name, (sql, _) in diagnostic.CHECKS.items() if sql == statement)
        return catalog[section]

    monkeypatch.setattr(diagnostic, "rows", read)
    return catalog, revisions, connection


def test_complete_baseline_is_verified(baseline_fixture):
    _, _, connection = baseline_fixture
    report = diagnostic.inspect(connection)
    assert report["verdict"] == "verified"
    assert report["baseline"]["alembic"]["row_count"] == 1
    assert connection.rolled_back


@pytest.mark.parametrize("versions", [["other"], ["d2b7a4e9310c", "other"], []])
def test_revision_mismatch(baseline_fixture, versions):
    _, revisions, connection = baseline_fixture
    revisions[:] = [{"version_num": value} for value in versions]
    report = diagnostic.inspect(connection)
    assert report["baseline"]["alembic"]["status"] == "mismatch"
    assert report["baseline"]["alembic"]["row_count"] == len(versions)
    assert report["verdict"] == "incomplete"


@pytest.mark.parametrize(("section", "expected", "mutate"), [
    ("tables", "mismatch", lambda rows: rows.pop()),
    ("tables", "mismatch", lambda rows: rows.append(dict(rows[0], table_name="payments"))),
    ("columns", "mismatch", lambda rows: rows[0].update(type="bigint")),
    ("constraints", "mismatch", lambda rows: rows.pop()),
    ("indexes", "mismatch", lambda rows: rows.pop()),
    ("triggers", "mismatch", lambda rows: rows.append(dict(table_name="users", name="unexpected",
                                                          definition="secret-example", enabled="O"))),
    ("authority", "unavailable", lambda rows: rows.pop()),
    ("schema_privileges", "unavailable", lambda rows: rows.pop()),
    ("table_privileges", "unavailable", lambda rows: rows.pop()),
    ("sessions", "mismatch", lambda rows: rows[0].update(visibly_blocked=1)),
    ("sessions", "unavailable", lambda rows: rows[0].update(state_unavailable=1)),
])
def test_baseline_drift_or_missing_evidence_blocks_verdict(baseline_fixture, section, expected, mutate):
    catalog, _, connection = baseline_fixture
    mutate(catalog[section])
    report = diagnostic.inspect(connection)
    result = report["baseline"].get(section, report["catalog"][section])
    assert result["status"] == expected
    assert report["verdict"] == "incomplete"
    assert "secret-example" not in str(report)
    if section == "tables" and "payments" in result["unexpected"]:
        assert report["baseline"]["payments"]["transfers"]["status"] == "unavailable"


def test_browser_table_access_detected_without_claiming_api_exposure(baseline_fixture):
    catalog, _, connection = baseline_fixture
    row = next(r for r in catalog["browser_columns"] if r["principal"]=="anon" and r["relation"]=="users")
    row["can_read"] = True
    report = diagnostic.inspect(connection)
    assert report["catalog"]["browser_sql_risk"]["rows"] == [("anon", "users")]
    assert report["verdict"] == "incomplete"


def test_unexpected_payment_checks_are_aggregate_only(baseline_fixture, monkeypatch):
    catalog, _, connection = baseline_fixture
    for table, names in {
        "payments": "status method transaction_reference appointment_id receipt_path submitted_at reviewed_at submission_revision reconciliation_state",
        "payment_events": "payment_id",
    }.items():
        catalog["tables"].append(dict(table_name=table))
        catalog["columns"].extend(dict(table_name=table, name=column) for column in names.split())
    queries = []

    def read(_, sql, args=()):
        assert sql in (diagnostic.preflight.DUPLICATES, diagnostic.preflight.RECONCILIATION,
                       diagnostic.preflight.DECISIONS, "SELECT count(*) AS payment_count FROM public.payments")
        queries.append(sql)
        return [{"count": 0}]

    monkeypatch.setattr(diagnostic, "rows", read)
    result = diagnostic.payment_status(connection, catalog)
    assert result["status"] == "mismatch"
    assert len(queries) == 4


def test_xp_check_keeps_spaces_inside_string_literals(baseline_fixture):
    catalog, _, connection = baseline_fixture
    row = next(r for r in catalog["constraints"] if r["name"] == "ck_xp_events_path")
    row["expression"] = row["expression"].replace("'academic'", "'aca demic'")
    report = diagnostic.inspect(connection)
    assert report["baseline"]["constraints"]["incompatible"] == ["ck_xp_events_path"]
    assert "aca demic" not in str(report)
