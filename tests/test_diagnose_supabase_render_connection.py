from contextlib import nullcontext

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
                             "database_name": "postgres"}]
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
    assert report["tables"] == {
        "status": "unavailable", "reason": "PostgreSQL permission or catalog access denied.",
    }
    assert report["alembic"]["matches_expected"]
    assert report["financial_runtime_exists"] is False


def test_success_output_has_no_connection_arguments(monkeypatch, capsys):
    monkeypatch.setattr(diagnostic.psycopg, "connect", lambda **_: nullcontext(Connection()))
    assert diagnostic.main({"DATABASE_URL": URL}) == 0
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
    (psycopg.OperationalError, "certificate verify failed", "TLS certificate or hostname verification failed"),
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
        return [{"read_only": "off", "isolation": "repeatable read", "database_name": "postgres"}]

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
