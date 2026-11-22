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
    assert report["tables"] == {"status": "unavailable"}
    assert report["alembic"]["matches_expected"]
    assert report["financial_runtime_exists"] is False


def test_success_output_has_no_connection_arguments(monkeypatch, capsys):
    monkeypatch.setattr(diagnostic.psycopg, "connect", lambda **_: nullcontext(Connection()))
    assert diagnostic.main({"DATABASE_URL": URL}) == 0
    captured = capsys.readouterr()
    assert URL not in captured.out + captured.err
    assert "secret-example" not in captured.out + captured.err
    assert "repeatable read" in captured.out
