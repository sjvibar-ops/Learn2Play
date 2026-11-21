"""Inspect the approved Learn2Play Development database without writing to it."""

import getpass
import json
import os
from pathlib import Path
import re
import sys
import warnings

# The launcher uses isolated Python, so add only this repository's script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import supabase_production_preflight as catalog


EXPECTED_HEAD = "83ca40c9f279"
DIRECT_HOST = f"db.{catalog.DEVELOPMENT}.supabase.co"
SEOUL_POOLER = re.compile(r"aws-\d+-ap-northeast-2\.pooler\.supabase\.com")
FINANCIAL_FUNCTIONS = """
SELECT expected.signature, p.oid IS NOT NULL AS present,
       pg_catalog.has_function_privilege(p.oid, 'EXECUTE') AS executable,
       p.prorettype = pg_catalog.to_regtype(expected.result_type) AS result_matches
FROM (VALUES ('public.phase2_approval_ready()', 'pg_catalog.bool'),
             ('public.phase2_allocation_complete(integer,integer)', 'pg_catalog.bool'),
             ('public.seal_ledger_transaction(bigint)', 'pg_catalog.void'))
     AS expected(signature, result_type)
LEFT JOIN pg_catalog.pg_proc p
  ON p.oid = pg_catalog.to_regprocedure(expected.signature) AND p.prokind = 'f'
"""


def approved_host(host):
    return host == DIRECT_HOST or bool(SEOUL_POOLER.fullmatch(host))


def connection_parameters(raw, args, parse):
    raw = re.sub(r"^postgres(?:ql\+psycopg2?)?://", "postgresql://", raw, count=1)
    if not raw.startswith("postgresql://"):
        raise catalog.Blocked("An explicit PostgreSQL URL is required.")
    try:
        parameters = parse(raw)
    except Exception:
        raise catalog.Blocked("PostgreSQL URL is malformed.") from None
    if set(parameters) - {"host", "port", "dbname", "user", "password", "sslmode"}:
        raise catalog.Blocked("Additional connection options are not allowed.")
    host = parameters.get("host", "").lower()
    user = parameters.get("user", "")
    if catalog.PRODUCTION in host or catalog.PRODUCTION in user:
        raise catalog.Blocked("Production project rejected.")
    if not approved_host(host) or host != args.expected_host.lower():
        raise catalog.Blocked("URL does not match the approved Development host.")
    expected_user = args.expected_runtime_role
    if host != DIRECT_HOST:
        expected_user += "." + catalog.DEVELOPMENT
    if user != expected_user:
        raise catalog.Blocked("Login does not match the approved Development project and role.")
    if (parameters.get("port", "5432") != str(args.expected_port)
            or parameters.get("dbname") != "postgres" or not parameters.get("password")):
        raise catalog.Blocked("URL differs from the approved port or database, or lacks a password.")
    if parameters.get("sslmode", "verify-full") != "verify-full":
        raise catalog.Blocked("The diagnostic requires sslmode=verify-full.")
    return dict(host=host, port=args.expected_port, dbname="postgres", user=user,
                password=parameters["password"], sslmode="verify-full", gssencmode="disable",
                connect_timeout=10)


def section(**details):
    return dict(problems=[], review=[], unverified=[], **details)


def summarize(report):
    for key in ("problems", "unverified", "review"):
        if report[key]:
            return {"problems": "BLOCKER", "unverified": "NOT VERIFIED", "review": "WARNING"}[key]
    return "PASS"


def inspect_target(connection, args, schema, database, storage, driver_error):
    if not connection.pgconn.ssl_in_use:
        raise catalog.Blocked("TLS is not active. No inspection was performed.")
    connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        identity = connection.execute(catalog.IDENTITY).fetchone()
        catalog.verify_identity(identity, args)
        try:
            relations, columns = catalog.inspect(
                connection, args, identity, *schema, database, project_reference=catalog.DEVELOPMENT,
            )
            functions = connection.execute(FINANCIAL_FUNCTIONS).fetchall()
            database["financial_function_signatures"] = functions
            for function in functions:
                if not function["present"] or not function["result_matches"] or not function["executable"]:
                    database["problems"].append(
                        f"Required financial function missing, incompatible, or not executable: {function['signature']}."
                    )
        except driver_error:
            database["unverified"].append("A database metadata query failed. Driver details were suppressed.")
            storage["unverified"].append("Storage inspection was not attempted after the database query failed.")
            return
        storage["policies"] = [policy for policy in database["policies"] if policy["schema"] == "storage"]
        try:
            catalog.inspect_storage(connection, args.receipt_bucket, relations, columns, storage)
        except driver_error:
            storage["unverified"].append("The SQL role could not complete the Storage metadata query.")
    finally:
        connection.execute("ROLLBACK")


def main():
    parser = catalog.SafeArguments(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-port", type=int, required=True)
    parser.add_argument("--expected-runtime-role", required=True)
    parser.add_argument("--receipt-bucket", default="payment-receipts")
    args = parser.parse_args()
    if (not approved_host(args.expected_host.lower())
            or not 1 <= args.expected_port <= 65535
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", args.expected_runtime_role)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", args.receipt_bucket)
            or args.receipt_bucket in ("avatars", "credentials")
            or not sys.stdin.isatty() or not sys.stderr.isatty()):
        raise catalog.Blocked("Supply an approved Development endpoint, SQL role and bucket in an interactive terminal.")
    if any(key.upper().startswith("PG") and key.upper() != "PGSSLROOTCERT" for key in os.environ):
        raise catalog.Blocked("Ambient libpq overrides are present. Use an approved clean terminal.")

    from psycopg import Error, connect
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row

    head, revisions = catalog.migration_graph()
    if head != EXPECTED_HEAD:
        raise catalog.Blocked("Local migration head differs from this diagnostic's reviewed Development head.")
    schema = (head, revisions, *catalog.local_schema())
    print("Expected Development project:", catalog.DEVELOPMENT)
    print("Approved endpoint:", args.expected_host, args.expected_port, "postgres/public")
    if input("Type APPROVE DEVELOPMENT READ ONLY to connect: ") != "APPROVE DEVELOPMENT READ ONLY":
        raise catalog.Blocked("Development consent was not supplied. No connection made.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        parameters = connection_parameters(
            getpass.getpass("Development DATABASE_URL (hidden): "), args, conninfo_to_dict,
        )
    database = section(application_schema={}, expected_head=head)
    storage = section()
    with connect(**parameters, autocommit=True, row_factory=dict_row) as connection:
        del parameters
        inspect_target(connection, args, schema, database, storage, Error)
    database["status"] = summarize(database)
    storage["status"] = summarize(storage)
    overall = next(status for status in ("BLOCKER", "NOT VERIFIED", "WARNING", "PASS")
                   if status in (database["status"], storage["status"]))
    print(json.dumps(dict(
        status=overall, database=database, storage=storage,
        limitations=[
            "PASS covers inspected metadata only. It is not deployment approval.",
            "Catalog checks do not prove policy behavior, function bodies, or effective access to individual rows.",
            "If the runtime SQL role cannot inspect bucket metadata safely, verify it separately without elevating this connection.",
            "The diagnostic never reads upload contents or exercises the Storage API.",
        ],
    ), indent=2, ensure_ascii=True))
    return 0 if overall in ("PASS", "WARNING") else 1


def cli():
    try:
        return main()
    except catalog.Blocked as exc:
        print("BLOCKER: " + str(exc))
    except (Exception, KeyboardInterrupt):
        print("BLOCKER: Diagnostic stopped. Connection and error details were suppressed.")
    return 2


if __name__ == "__main__":
    sys.exit(cli())
