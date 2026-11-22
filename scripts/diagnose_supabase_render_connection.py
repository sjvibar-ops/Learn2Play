"""Inspect the approved Render-style Development SQL connection without writes."""

import json
import os
from pathlib import Path
import re
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))
from supabase_development_backup import read_development_url

PROJECT = "pxiwoowpoewxvnsjwfdw"
EXPECTED_REVISION = "d2b7a4e9310c"
ROLES = ("anon", "authenticated", "service_role", "postgres", "learn2play_financial_runtime")
IDENTITY = """
SELECT current_database() AS database_name, current_user AS effective_role,
       session_user AS session_role, current_setting('server_version') AS server_version,
       current_schema() AS schema_name, current_setting('transaction_read_only') AS read_only,
       current_setting('transaction_isolation') AS isolation
"""
TABLES = """
SELECT c.relname AS table_name, pg_get_userbyid(c.relowner) AS owner,
       c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS rls_forced,
       row_security_active(c.oid) AS rls_active_for_connection
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname
"""
ROLE_FILTER = "(r.rolname = ANY(%s) OR r.rolname = current_user OR r.rolname = session_user)"
FUNCTION_FILTER = """
n.nspname = 'public' AND p.proname IN
('phase2_approval_ready', 'phase2_allocation_complete', 'seal_ledger_transaction')
"""
CHECKS = {
    "roles": (f"""
SELECT r.rolname, r.rolsuper, r.rolbypassrls, r.rolinherit, r.rolcreaterole,
       r.rolcreatedb, r.rolcanlogin, pg_has_role(current_user, r.oid, 'MEMBER') AS effective_member,
       pg_has_role(current_user, r.oid, 'USAGE') AS effective_inherited,
       has_schema_privilege(r.oid, 'public', 'USAGE') AS public_schema_usage
FROM pg_catalog.pg_roles r WHERE {ROLE_FILTER} ORDER BY r.rolname
""", (list(ROLES),)),
    "tables": (TABLES, ()),
    "table_privileges": (f"""
SELECT r.rolname, c.relname, ARRAY(
    SELECT privilege FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'])
    AS privileges(privilege) WHERE has_table_privilege(r.oid, c.oid, privilege)
) AS privileges
FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE {ROLE_FILTER} AND n.nspname = 'public' AND c.relkind IN ('r','p')
ORDER BY r.rolname, c.relname
""", (list(ROLES),)),
    "sequences": (f"""
SELECT r.rolname, c.relname, pg_get_userbyid(c.relowner) AS owner, ARRAY(
    SELECT privilege FROM unnest(ARRAY['USAGE','SELECT','UPDATE']) AS privileges(privilege)
    WHERE has_sequence_privilege(r.oid, c.oid, privilege)
) AS privileges
FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE {ROLE_FILTER} AND n.nspname = 'public' AND c.relkind = 'S'
ORDER BY r.rolname, c.relname
""", (list(ROLES),)),
    "functions": (f"""
SELECT r.rolname, p.oid::regprocedure::text AS signature,
       pg_get_userbyid(p.proowner) AS owner,
       has_function_privilege(r.oid, p.oid, 'EXECUTE') AS can_execute
FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE {ROLE_FILTER} AND {FUNCTION_FILTER} ORDER BY r.rolname, p.oid
""", (list(ROLES),)),
    # Report policy metadata only. Expressions can contain private literals.
    "policies": ("""
SELECT tablename, policyname, permissive, roles, cmd FROM pg_catalog.pg_policies
WHERE schemaname = 'public' ORDER BY tablename, policyname
""", ()),
}


class Blocked(Exception):
    """A diagnostic safety gate was not satisfied."""


def parameters(raw):
    normalized = re.sub(r"^postgres(?:ql(?:\+psycopg2?)?)?://", "postgresql://", raw, count=1)
    if not normalized.startswith("postgresql://") or any(c.isspace() for c in normalized):
        raise Blocked
    values = conninfo_to_dict(normalized)
    if (set(values) - {"host", "port", "dbname", "user", "password", "sslmode"}
            or not re.fullmatch(r"aws-\d+-[a-z0-9-]+\.pooler\.supabase\.com", values.get("host", ""))
            or values.get("port", "5432") != "5432"
            or values.get("dbname") != "postgres"
            or values.get("user") != f"postgres.{PROJECT}"
            or not values.get("password")
            or values.get("sslmode", "require") not in {"require", "verify-ca", "verify-full"}):
        raise Blocked
    # Pin the Development session pooler and strengthen TLS without printing the URL.
    return dict(values, port=5432, sslmode="verify-full", sslrootcert="system",
                gssencmode="disable", connect_timeout=8,
                options="-c default_transaction_read_only=on -c statement_timeout=8000 -c lock_timeout=2000")


def rows(connection, sql, args=()):
    return connection.execute(sql, args).fetchall()


def inspect(connection):
    connection.read_only = True
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    # The outer transaction is always rolled back. Nested scopes isolate catalog errors.
    try:
        identity = rows(connection, IDENTITY)[0]
        if (identity["read_only"], identity["isolation"], identity["database_name"]) != (
            "on", "repeatable read", "postgres"
        ):
            raise Blocked
        report = {"identity": identity}
        for name, (sql, args) in CHECKS.items():
            try:
                with connection.transaction():
                    report[name] = rows(connection, sql, args)
            except psycopg.Error:
                report[name] = {"status": "unavailable"}
        if isinstance(report["roles"], list):
            report["financial_runtime_exists"] = any(
                role["rolname"] == "learn2play_financial_runtime" for role in report["roles"]
            )
        try:
            with connection.transaction():
                exists = rows(connection, "SELECT to_regclass('public.alembic_version') IS NOT NULL AS present")
                revisions = (rows(connection, "SELECT version_num FROM public.alembic_version ORDER BY version_num")
                             if exists[0]["present"] else None)
                report["alembic"] = {
                    "rows": revisions, "expected": EXPECTED_REVISION,
                    "matches_expected": revisions == [{"version_num": EXPECTED_REVISION}],
                }
        except psycopg.Error:
            report["alembic"] = {"status": "unavailable"}
        return report
    finally:
        connection.rollback()


def main(environ=None):
    environ = os.environ if environ is None else environ
    try:
        # libpq environment defaults can override connection routing, TLS and options.
        if any(key.upper().startswith("PG") for key in environ):
            raise Blocked
        raw = environ.get("DATABASE_URL") or read_development_url()
        if not raw:
            raise Blocked
        options = parameters(raw)
        with psycopg.connect(**options, row_factory=dict_row) as connection:
            report = inspect(connection)
        output = json.dumps(report, indent=2, ensure_ascii=True)
        # Catalog text is normally identifiers. Refuse output if it includes supplied secrets.
        if any(secret and secret in output for secret in {raw, options["password"]}):
            raise Blocked
        print(output)
        return 0
    except (Exception, KeyboardInterrupt):
        print("Diagnostic stopped. Check the secure console, Development session pooler URL, "
              "verified TLS, and read-only connection availability.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
