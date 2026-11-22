"""Inspect the approved Render-style Development SQL connection without writes."""

import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))
from supabase_development_backup import read_development_url

PROJECT = "pxiwoowpoewxvnsjwfdw"
EXPECTED_REVISION = "d2b7a4e9310c"
CA_FILE_ENV = "LEARN2PLAY_SUPABASE_CA_FILE"
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


def root_certificate(environ):
    configured = environ.get(CA_FILE_ENV)
    if configured is None:
        if any(key.upper() in {"SSL_CERT_FILE", "SSL_CERT_DIR"} for key in environ):
            raise Blocked("OpenSSL default trust is overridden. Use the dedicated Development CA file setting.")
        return "system"

    path = Path(configured)
    if not path.is_absolute() or configured.startswith(("//", chr(92) * 2)) or chr(0) in configured:
        raise Blocked("Invalid Development CA configuration. Supply an absolute local file path.")
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise Blocked("Invalid Development CA configuration. Supply a regular PEM CA file.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(path))
    except FileNotFoundError:
        raise Blocked("Development CA file is missing.") from None
    except ssl.SSLError:
        raise Blocked("Invalid Development CA file. Expected a readable PEM CA certificate.") from None
    except OSError:
        raise Blocked("Development CA file could not be read.") from None
    if not context.cert_store_stats()["x509_ca"]:
        raise Blocked("Invalid Development CA file. No CA certificate was found.")
    return str(path)


def parameters(raw, root_cert="system"):
    normalized = re.sub(r"^postgres(?:ql(?:\+psycopg2?)?)?://", "postgresql://", raw, count=1)
    if not normalized.startswith("postgresql://") or any(c.isspace() for c in normalized):
        raise Blocked("Invalid connection URL.")
    try:
        values = conninfo_to_dict(normalized)
    except (psycopg.Error, ValueError):
        raise Blocked("Invalid connection URL.") from None
    if set(values) - {"host", "port", "dbname", "user", "password", "sslmode"}:
        raise Blocked("Unexpected connection URL options.")
    if values.get("host") != "aws-0-ap-northeast-2.pooler.supabase.com":
        raise Blocked("Unexpected host. Use the approved Development session pooler.")
    if values.get("port", "5432") != "5432":
        raise Blocked("Unexpected port. The session pooler requires port 5432.")
    if values.get("dbname") != "postgres":
        raise Blocked("Unexpected database name.")
    if values.get("user") != f"postgres.{PROJECT}":
        raise Blocked("Unexpected username or Development project reference.")
    if not values.get("password"):
        raise Blocked("Invalid connection URL. A password is required.")
    if values.get("sslmode", "require") not in {"require", "verify-ca", "verify-full"}:
        raise Blocked("Unsupported TLS mode. Certificate and hostname verification are mandatory.")
    # Pin the Development session pooler and strengthen TLS without printing the URL.
    return dict(values, port=5432, sslmode="verify-full", sslrootcert=root_cert,
                gssencmode="disable", connect_timeout=8,
                options="-c default_transaction_read_only=on -c statement_timeout=8000 -c lock_timeout=2000")


def database_failure(error, connecting):
    """Classify known driver failures without returning driver-provided text."""
    state = error.sqlstate or ""
    if state.startswith("28"):
        return "Database authentication failed."
    if state == "42501":
        return "PostgreSQL permission or catalog access denied."
    if state == "25006":
        return "Read-only transaction setup or verification failed."
    if state == "57014":
        return "PostgreSQL query was canceled or timed out."
    if state == "55P03":
        return "PostgreSQL lock could not be acquired before the deadline."
    if connecting:
        # libpq connection failures frequently have no SQLSTATE. Use only
        # fixed markers internally. The message may contain a full URL.
        message = str(error).lower()
        markers = (
            (("unsupported startup parameter", "unrecognized startup parameter"), "The pooler rejected a startup parameter."),
            (("does not match host name", "hostname mismatch", "does not match any host name"),
             "TLS hostname verification failed."),
            (("could not load system root certificate paths",),
             "OpenSSL could not load its default CA trust paths."),
            (("root certificate file", "locate root certificate"),
             "TLS root certificate file is missing or could not be loaded."),
            (("certificate has expired", "certificate expired"),
             "TLS certificate in the chain has expired."),
            (("certificate is not yet valid",), "TLS certificate in the chain is not yet valid."),
            (("unable to get local issuer", "unable to verify the first certificate",
              "self-signed certificate", "self signed certificate", "certificate untrusted"),
             "TLS certificate chain could not be linked to a trusted CA."),
            (("invalid ca certificate", "certificate signature failure", "invalid certificate chain"),
             "TLS certificate chain validation failed."),
            (("certificate verify failed", "certificate verification failed", "certificate file"),
             "TLS certificate or hostname verification failed (detail unavailable)."),
            (("could not translate host name", "name or service not known", "getaddrinfo",
              "temporary failure in name resolution", "no such host is known"), "DNS resolution failed."),
            (("connection timeout", "connection timed out", "timeout expired"), "Database connection timed out."),
            (("password authentication failed", "authentication failed", "no password supplied",
              "tenant or user not found"), "Database authentication failed."),
            (("connection refused",), "Database connection refused."),
        )
        if isinstance(error, psycopg.errors.ConnectionTimeout):
            return "Database connection timed out."
        for fragments, category in markers:
            if any(fragment in message for fragment in fragments):
                return category
        return "Unexpected connection error (unclassified)."
    return "PostgreSQL diagnostic query or transaction error (unclassified)."


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
            raise Blocked("Read-only transaction or database identity verification failed.")
        report = {"identity": identity}
        for name, (sql, args) in CHECKS.items():
            try:
                with connection.transaction():
                    report[name] = rows(connection, sql, args)
            except psycopg.Error as error:
                report[name] = {"status": "unavailable", "reason": database_failure(error, connecting=False)}
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
        except psycopg.Error as error:
            report["alembic"] = {"status": "unavailable", "reason": database_failure(error, connecting=False)}
        return report
    finally:
        connection.rollback()


def main(environ=None):
    environ = os.environ if environ is None else environ
    connecting = False
    try:
        # libpq environment defaults can override connection routing, TLS and options.
        if any(key.upper().startswith("PG") for key in environ):
            raise Blocked("Conflicting PostgreSQL environment configuration.")
        raw = environ.get("DATABASE_URL")
        if not raw:
            try:
                raw = read_development_url()
            except Exception:
                raise Blocked("Secure console input failed. Use an interactive Windows console.") from None
        if not raw:
            raise Blocked("Missing database connection URL.")
        options = parameters(raw, root_certificate(environ))
        connecting = True
        with psycopg.connect(**options, row_factory=dict_row) as connection:
            connecting = False
            report = inspect(connection)
        output = json.dumps(report, indent=2, ensure_ascii=True)
        # Catalog text is normally identifiers. Refuse output if it includes supplied secrets.
        if any(secret and secret in output for secret in {raw, options["password"]}):
            raise Blocked("Diagnostic output withheld because it may contain sensitive text.")
        print(output)
        return 0
    except Blocked as error:
        category = str(error)
    except psycopg.Error as error:
        category = database_failure(error, connecting)
    except KeyboardInterrupt:
        category = "Diagnostic canceled."
    except Exception:
        category = "Unexpected diagnostic error (unclassified)."
    print(f"Diagnostic stopped. {category}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
