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
import render_development_preflight_catalog as preflight


def dashboard_models():
    # Isolated Python does not add the repository directory to sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from models import Appointment, MentorAvailability, Payment, Report, RoleApplication, User, XpEvent

    return preflight.model_columns((User, RoleApplication, Appointment, Payment, XpEvent,
                                    MentorAvailability, Report))


PROJECT = "pxiwoowpoewxvnsjwfdw"
EXPECTED_REVISION = "d2b7a4e9310c"
EXPECTED_HEAD = "83ca40c9f279"
CA_FILE_ENV = "LEARN2PLAY_SUPABASE_CA_FILE"
ROLES = ("anon", "authenticated", "service_role", "postgres", "learn2play_financial_runtime")
IDENTITY = """
SELECT current_database() AS database_name, current_user AS effective_role,
       session_user AS session_role, current_setting('server_version') AS server_version,
       current_schema() AS schema_name, current_setting('transaction_read_only') AS read_only,
       current_setting('transaction_isolation') AS isolation,
       pg_catalog.current_schemas(true)::text[] = ARRAY['pg_catalog']::text[] AS catalog_path_verified
"""
TABLES = """
SELECT c.relname AS table_name, c.relkind, pg_get_userbyid(c.relowner) AS owner,
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
    # Report policy metadata and collect definitions separately for fingerprinting.
    "policies": ("""
SELECT tablename, policyname, permissive, roles, cmd FROM pg_catalog.pg_policies
WHERE schemaname = 'public' ORDER BY tablename, policyname
""", ()),
}

CHECKS.update({name: (sql, ()) for name, sql in preflight.CATALOG.items()})


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
                options="-c default_transaction_read_only=on -c statement_timeout=8000 -c lock_timeout=2000 -c search_path=pg_catalog")


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



def unavailable(reason):
    return {"status": "unavailable", "reason": reason}


def catalog_rows(connection, sql, args=()):
    try:
        with connection.transaction():
            return rows(connection, sql, args)
    except psycopg.Error as error:
        return unavailable(database_failure(error, connecting=False))


def assess_baseline(data):
    checks = {}
    compare = {
        "tables": lambda rows: preflight.inventory(
            {r["table_name"] for r in rows}, preflight.BASE_TABLES,
            [r["table_name"] for r in rows if r["owner"] != "postgres" or r["relkind"] != "r"
             or r["rls_enabled"] or r["rls_forced"]]),
        "columns": preflight.compare_columns,
        "constraints": preflight.compare_constraints,
        "indexes": preflight.compare_indexes,
        "triggers": lambda rows: preflight.inventory(
            {(r["table_name"], r["name"]) for r in rows}, set()),
        "sequences": lambda rows: preflight.inventory(
            {r["relname"] for r in rows}, {f"{table}_id_seq" for table in preflight.ID_TABLES},
            sorted({r["relname"] for r in rows if r["owner"] != "postgres"})),
        "roles": lambda rows: preflight.inventory(
            {r["rolname"] for r in rows}, set(preflight.PRINCIPALS),
            [r["rolname"] for r in rows if r["rolname"] == "postgres" and not (
                r["rolbypassrls"] and r["rolcanlogin"]
            )]),
    }
    for name, assess in compare.items():
        result = data[name]
        checks[name] = assess(result) if isinstance(result, list) else result
        if name == "indexes" and checks[name]["status"] != "unavailable":
            unexpected = checks[name]["unexpected"]
            checks[name]["unexpected"] = [{"table": key[0], "keys_redacted": True} for key in unexpected]
    for name in ("views", "routines"):
        result = data[name]
        checks[name] = (preflight.inventory({r["name"] for r in result}, set())
                        if isinstance(result, list) else result)
    return checks


def catalog_sections(data):
    expected_principals = set(preflight.PRINCIPALS) | {"PUBLIC"}
    tables = {r["table_name"] for r in data["tables"]} if isinstance(data["tables"], list) else set()
    sequences = {f"{table}_id_seq" for table in preflight.ID_TABLES}
    sections = {name: preflight.status(value) for name, value in data.items()}
    columns = data["columns"]
    if isinstance(columns, list):
        sections["columns"] = preflight.status(preflight.digest_definitions(
            columns, {"default_expression"}))
    for name, fields in {
        "constraints": {"expression"}, "triggers": {"definition"},
        "policy_definitions": {"qual", "with_check"},
    }.items():
        if isinstance(data[name], list):
            sections[name] = preflight.status(preflight.digest_definitions(data[name], fields))
    if isinstance(data["indexes"], list):
        sections["indexes"] = preflight.status([
            {key: value for key, value in row.items() if key not in {"columns", "predicate"}} for row in data["indexes"]
        ])

    # Privilege queries are cross products. A partial result cannot prove coverage.
    def require_matrix(name, expected, key):
        result = data[name]
        if isinstance(result, list) and {key(row) for row in result} != expected:
            sections[name] = unavailable("Incomplete privilege or ownership inventory.")

    require_matrix("table_privileges", {(p, t) for p in preflight.PRINCIPALS for t in tables},
                   lambda r: (r["rolname"], r["relname"]))
    require_matrix("sequences", {(p, t) for p in preflight.PRINCIPALS for t in sequences},
                   lambda r: (r["rolname"], r["relname"]))
    require_matrix("schema_privileges", expected_principals, lambda r: r["principal"])
    require_matrix("authority", tables | sequences, lambda r: r["relation"])
    require_matrix("public_table_privileges", tables, lambda r: r["relation"])
    if isinstance(columns, list) and isinstance(data["browser_columns"], list) and isinstance(data["views"], list):
        expected_columns = {(r["table_name"], r["name"]) for r in columns}
        # Public views are separately checked for row grants and owner semantics.
        observed = {(r["principal"], r["relation"], r["column_name"])
                    for r in data["browser_columns"] if r["relkind"] in {"r", "p"}}
        expected = {(p, t, col) for p in expected_principals for t, col in expected_columns}
        if observed != expected or not expected_columns or {t for t, _ in expected_columns} != tables:
            sections["browser_columns"] = unavailable("Incomplete table-column privilege inventory.")
    for name, fields in (("views", ("name",)), ("routines", ("name", "argument_types", "prokind"))):
        if isinstance(data[name], list):
            observed = {(r["principal"], *(r[field] for field in fields)) for r in data[name]}
            expected = {(p, *signature) for _, *signature in observed for p in expected_principals}
            require_matrix(name, expected, lambda r: (r["principal"], *(r[field] for field in fields)))

    if isinstance(data["policies"], list) and isinstance(data["policy_definitions"], list):
        policy_keys = lambda rows: {(r["tablename"], r["policyname"]) for r in rows}
        if policy_keys(data["policies"]) != policy_keys(data["policy_definitions"]):
            sections["policy_definitions"] = unavailable("Incomplete policy definition inventory.")

    if sections["browser_columns"]["status"] == "verified" and isinstance(data["tables"], list):
        unprotected = {r["table_name"] for r in data["tables"] if not r["rls_enabled"]}
        sql_paths = sorted({(r["principal"], r["relation"]) for r in data["browser_columns"]
                            if r["principal"] in {"PUBLIC", "anon", "authenticated"}
                            and r["relation"] in unprotected
                            and (r["can_read"] or r["can_insert"] or r["can_update"])})
        sections["browser_sql_risk"] = preflight.status(
            sql_paths, "mismatch" if sql_paths else "verified",
            limitation="SQL access on RLS-disabled tables. HTTP/API reachability requires separate review.",
        )
    else:
        sections["browser_sql_risk"] = unavailable("Required browser SQL privilege evidence is incomplete.")

    schema, authority = data["schema_privileges"], data["authority"]
    if sections["schema_privileges"]["status"] == sections["authority"]["status"] == "verified":
        runtime = next(r for r in schema if r["principal"] == "postgres")
        unsuitable = [r["relation"] for r in authority if not r["owner_authority"]
                      or (r["relkind"] != "S" and not (r["can_reference"] and r["can_trigger"]))]
        sections["executor"] = preflight.status(
            [], "mismatch" if unsuitable or not (runtime["can_use"] and runtime["can_create"]) else "verified",
            incompatible=unsuitable,
            limitation="Catalog prerequisites only. Actual DDL and grant authority require a rehearsal.",
        )
    else:
        sections["executor"] = unavailable("Required schema or object authority could not be established.")

    sessions = data["sessions"]
    if isinstance(sessions, list):
        if len(sessions) != 1 or sessions[0]["state_unavailable"]:
            sections["sessions"] = unavailable("Monitoring visibility is incomplete.")
        elif sessions[0]["idle_in_transaction"] or sessions[0]["visibly_blocked"]:
            sections["sessions"] = preflight.status(sessions, "mismatch")
    sections["sessions"]["limitation"] = (
        "Point-in-time PostgreSQL backend visibility, not pooler client counts. "
        "A later session or lock may still interfere."
    )
    return sections


def payment_status(connection, data):
    tables, columns = data["tables"], data["columns"]
    if not isinstance(tables, list) or not isinstance(columns, list):
        return unavailable("Payment object inventory is incomplete.")
    names = {r["table_name"] for r in tables}
    present = sorted(names & preflight.PENDING_PAYMENTS)
    fields = {}
    for row in columns:
        fields.setdefault(row["table_name"], set()).add(row["name"])
    pending_column = "payment_required" in fields.get("appointments", set())
    result = preflight.status([], "mismatch" if present or pending_column else "verified",
                              unexpected_tables=present, payment_required_present=pending_column)
    if "payments" in present:
        result["payment_count"] = preflight.status(catalog_rows(
            connection, "SELECT count(*) AS payment_count FROM public.payments"))
        if "reconciliation_state" in fields.get("payments", set()):
            result["recorded_decisions"] = preflight.status(catalog_rows(connection, preflight.DECISIONS))
        else:
            result["recorded_decisions"] = unavailable("Reconciliation state column is absent.")
        if {"status", "method", "transaction_reference"} <= fields.get("payments", set()):
            result["transfers"] = preflight.status(catalog_rows(connection, preflight.DUPLICATES))
        else:
            result["transfers"] = unavailable("Required payment columns are absent.")
        if ({"status", "appointment_id", "receipt_path", "submitted_at", "reviewed_at",
             "submission_revision", "reconciliation_state"} <= fields.get("payments", set())
                and {"id", "status"} <= fields.get("appointments", set())
                and "payment_events" in present and "payment_id" in fields.get("payment_events", set())):
            result["reconciliation"] = preflight.status(catalog_rows(connection, preflight.RECONCILIATION))
        else:
            result["reconciliation"] = unavailable("Required reconciliation tables or columns are absent.")
    return result


def inspect(connection):
    connection.read_only = True
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    # The outer transaction is always rolled back. Nested scopes isolate catalog errors.
    try:
        identity = rows(connection, IDENTITY)[0]
        if (identity["read_only"], identity["isolation"], identity["database_name"]) != (
            "on", "repeatable read", "postgres"
        ) or identity.get("catalog_path_verified") is not True:
            raise Blocked("Read-only transaction or database identity verification failed. Check the catalog search path.")
        data = {name: catalog_rows(connection, sql, args) for name, (sql, args) in CHECKS.items()}
        baseline = assess_baseline(data)
        sections = catalog_sections(data)
        present = catalog_rows(connection, "SELECT to_regclass('public.alembic_version') IS NOT NULL AS present")
        if not isinstance(present, list) or len(present) != 1:
            baseline["alembic"] = unavailable("Could not establish Alembic metadata.")
        elif not present[0]["present"]:
            baseline["alembic"] = preflight.status([], "mismatch", row_count=0, table_present=False)
        else:
            revisions = catalog_rows(connection, "SELECT version_num FROM public.alembic_version ORDER BY version_num")
            if isinstance(revisions, list):
                valid = revisions == [{"version_num": EXPECTED_REVISION}]
                baseline["alembic"] = preflight.status(
                    revisions, "verified" if valid else "mismatch", row_count=len(revisions),
                    matches_expected=valid,
                )
            else:
                baseline["alembic"] = revisions
        baseline["payments"] = payment_status(connection, data)
        baseline["policies"] = (preflight.inventory(
            {(r["tablename"], r["policyname"]) for r in data["policies"]}, set())
            if isinstance(data["policies"], list) else data["policies"])
        baseline["runtime_identity"] = preflight.status(
            [], "verified" if identity.get("effective_role") == identity.get("session_role") == "postgres"
            else "mismatch",
        )
        dashboard = preflight.dashboard_schema(data, dashboard_models())
        revisions = [row["version_num"] for row in baseline["alembic"].get("rows", [])]
        try:
            graph = preflight.migration_graph(
                Path(__file__).resolve().parents[1] / "migrations" / "versions",
                revisions, EXPECTED_REVISION, EXPECTED_HEAD,
            )
        except (OSError, KeyError, SyntaxError, ValueError):
            graph = preflight.diagnostic_item("migration graph", "INCOMPLETE",
                                              reason="Could not establish local revision history.")
        complete = (all(section["status"] == "verified" for section in (*baseline.values(), *sections.values()))
                    and graph["status"] == "PASS"
                    and all(item["status"] in {"PASS", "WARNING"} for item in dashboard.values()))
        return {
            "identity": identity, "expected_revision": EXPECTED_REVISION, "expected_head": EXPECTED_HEAD,
            "baseline": baseline, "catalog": sections, "dashboard_schema": dashboard, "migration_graph": graph,
            "financial_runtime_exists": (any(r["rolname"] == "learn2play_financial_runtime"
                                             for r in data["roles"])
                                         if isinstance(data["roles"], list) else None),
            "verdict": "verified" if complete else "incomplete",
            "limitations": [
                "SQL privileges do not establish Supabase HTTP/API reachability. Review exposed schemas and RPC paths independently.",
                "SQL definitions that may contain private literals are fingerprinted or omitted rather than printed.",
                "This snapshot is not proof of DDL execution or migration approval.",
                "The diagnostic pins search_path to pg_catalog. Flask's runtime search path is not tested.",
                "An empty default ACL inventory does not remove built-in PostgreSQL privileges.",
            ],
        }
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
        if any(secret and (secret in output or json.dumps(secret)[1:-1] in output)
               for secret in {raw, options["password"]}):
            raise Blocked("Diagnostic output withheld because it may contain sensitive text.")
        print(output)
        return 0 if report["verdict"] == "verified" else 2
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
