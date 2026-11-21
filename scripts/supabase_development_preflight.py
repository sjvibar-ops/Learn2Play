"""Inspect the approved Learn2Play Development database without writing to it."""

import ast
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
DEFINITIONS = """
SELECT t.relname AS table_name, i.relname AS name, 'index' AS kind,
       pg_catalog.pg_get_expr(x.indpred, t.oid) AS expression
FROM pg_catalog.pg_index x JOIN pg_catalog.pg_class t ON t.oid=x.indrelid
JOIN pg_catalog.pg_class i ON i.oid=x.indexrelid
JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
WHERE n.nspname='public' AND t.relname=ANY(%s) AND x.indpred IS NOT NULL
UNION ALL
SELECT t.relname, c.conname, 'check', pg_catalog.pg_get_expr(c.conbin,t.oid)
FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
WHERE n.nspname='public' AND t.relname=ANY(%s) AND c.contype='c'
"""
FINANCIAL_TRIGGERS = """
SELECT t.relname AS table_name, g.tgname AS name,
       pg_catalog.pg_get_triggerdef(g.oid) AS definition
FROM pg_catalog.pg_trigger g JOIN pg_catalog.pg_class t ON t.oid=g.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
WHERE n.nspname='public' AND g.tgname=ANY(%s) AND NOT g.tgisinternal
"""
FINANCIAL_RUNTIME = """
SELECT pg_catalog.current_setting('session_replication_role') AS replication_role,
       pg_catalog.current_setting('default_transaction_isolation') AS default_isolation,
       pg_catalog.has_schema_privilege('public','USAGE') AS schema_usage,
       pg_catalog.has_column_privilege(t.oid,a.attnum,'UPDATE') AS update_sealed,
       pg_catalog.row_security_active(t.oid) AS rls_active
FROM pg_catalog.pg_class t JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid
WHERE n.nspname='public' AND t.relname='ledger_transactions'
 AND t.relkind IN ('r','p') AND a.attname='sealed' AND NOT a.attisdropped
"""
SEAL_POLICIES = """
SELECT p.polcmd AS command, p.polpermissive AS permissive,
       pg_catalog.pg_get_expr(p.polqual,t.oid) AS using_expression,
       pg_catalog.pg_get_expr(p.polwithcheck,t.oid) AS check_expression
FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class t ON t.oid=p.polrelid
JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
WHERE n.nspname='public' AND t.relname='ledger_transactions' AND p.polcmd IN ('*','w','r')
 AND EXISTS (SELECT 1 FROM pg_catalog.unnest(p.polroles) r(oid)
             WHERE CASE WHEN r.oid=0 THEN true ELSE pg_catalog.pg_has_role(current_user,r.oid,'USAGE') END)
"""
TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|::|<>|>=|<=|[A-Za-z_][A-Za-z_0-9]*|\d+|[^\s]")


def expression_tokens(expression):
    """Conservatively recognize simple deparser changes without ignoring grouping."""
    tokens = [t if t.startswith(("'", '"')) else t.lower() for t in TOKEN.findall(expression or "")]
    # PostgreSQL adds text casts when comparing varchar columns with string literals.
    # Recognize only the exact simple forms used in the relevant model predicates.
    if re.fullmatch(r"\(*\s*\(?(?:new\.|old\.)?[a-z_]+\)?::text\s*\)*\s*(?:=|<>)\s*'[^']*'::text\s*\)*",
                    expression or "", re.IGNORECASE):
        tokens = [t for i, t in enumerate(tokens)
                  if t not in ("(", ")", "::") and (i == 0 or tokens[i - 1] != "::")]
    while tokens and tokens[0] == "(" and tokens[-1] == ")":
        depth = 0
        for index, token in enumerate(tokens):
            depth += (token == "(") - (token == ")")
            if not depth:
                break
        if index != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


def compare_expression(expected, observed, label, report):
    if expected is None or observed is None:
        if expected != observed:
            report["problems"].append(f"{label}: required expression presence differs.")
            return "BLOCKER"
        return "MATCH"
    if expression_tokens(expected) != expression_tokens(observed):
        report["unverified"].append(f"{label}: expression differs or PostgreSQL deparsing requires review.")
        return "NOT VERIFIED"
    return "MATCH"


def check_definitions(connection, tables, dialect, report):
    from sqlalchemy import CheckConstraint

    names = sorted(tables)
    definitions = {(row["table_name"], row["kind"], row["name"]): row["expression"]
                   for row in connection.execute(DEFINITIONS, (names, names)).fetchall()}
    for table in tables.values():
        expected = {("check", constraint.name): constraint.sqltext for constraint in table.constraints
                    if isinstance(constraint, CheckConstraint)}
        expected.update({("index", index.name): index.dialect_options["postgresql"]["where"]
                         for index in table.indexes if index.dialect_options["postgresql"]["where"] is not None})
        details = report["application_schema"][table.name]
        details["expression_checks"] = {}
        for (kind, name), expression in sorted(expected.items()):
            details["expression_checks"][name] = compare_expression(
                str(expression.compile(dialect=dialect, compile_kwargs={"literal_binds": True})),
                definitions.get((table.name, kind, name)), f"{table.name}.{name}", report,
            )
        for column in details.get("columns", []):
            if column["status"] == "REVIEW DEFAULT":
                report["review"].append(f"{table.name}.{column['name']}: review server default.")


def trigger_details(definition):
    update = re.search(r"\bUPDATE OF (.+?) ON ", definition, re.IGNORECASE | re.DOTALL)
    condition = re.search(r"\bWHEN\s*\((.+)\)\s+EXECUTE\s+(?:FUNCTION|PROCEDURE)\b",
                          definition, re.IGNORECASE | re.DOTALL)
    columns = {column.strip().lower() for column in update[1].split(",")} if update else set()
    return columns, condition[1] if condition else None


def check_trigger_definitions(connection, report):
    expected = {}
    for revision in ("c83d9f2a71e4", "f78b2c01d4e6"):
        path = next((catalog.ROOT / "migrations" / "versions").glob(f"{revision}_*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            match = re.match(r"CREATE (?:CONSTRAINT )?TRIGGER (\w+) .+? ON public\.(\w+)",
                             node.value, re.DOTALL) if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
            if match:
                expected[match[2], match[1]] = trigger_details(node.value)
    for row in connection.execute(FINANCIAL_TRIGGERS, (sorted({name for _, name in expected}),)).fetchall():
        key = row["table_name"], row["name"]
        if key not in expected:
            continue
        columns, condition = trigger_details(row["definition"])
        expected_columns, expected_condition = expected[key]
        if columns != expected_columns:
            report["problems"].append(f"{row['name']}: UPDATE OF columns differ.")
        compare_expression(expected_condition, condition, row["name"], report)


def check_financial_runtime(runtime, policies, report):
    if runtime is None:
        report["problems"].append("Ledger transaction table or sealed column is missing.")
        return
    report["runtime_settings"] = runtime
    for field, expected in (("replication_role", "origin"), ("default_isolation", "read committed"),
                            ("schema_usage", True), ("update_sealed", True), ("rls_active", True)):
        if runtime[field] != expected:
            report["problems"].append(f"Financial runtime requirement differs: {field}.")
    if not runtime["rls_active"]:
        return
    for operation in ("r", "w"):
        relevant = [row for row in policies if row["command"] in ("*", operation)]
        permissive = [row for row in relevant if row["permissive"]]
        if not permissive:
            report["problems"].append(f"No applicable permissive ledger RLS policy for {operation}.")
            continue

        def unconditional(row):
            using = row["using_expression"]
            check = row["check_expression"] if row["check_expression"] is not None else using
            return (using is None or expression_tokens(using) == ["true"]) and (
                operation == "r" or check is None or expression_tokens(check) == ["true"])

        if not any(map(unconditional, permissive)) or any(
            not row["permissive"] and not unconditional(row) for row in relevant
        ):
            report["unverified"].append(f"Ledger RLS for {operation} depends on row conditions or restrictive policies.")


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


def aggregate(*sections):
    for report in sections:
        report["status"] = summarize(report)
    statuses = {report["status"] for report in sections}
    return next(status for status in ("BLOCKER", "NOT VERIFIED", "WARNING", "PASS") if status in statuses)


def inspect_target(connection, args, schema, database, financial, storage, storage_access, driver_error):
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
            check_definitions(connection, schema[2], schema[3], database)
        except driver_error:
            database["unverified"].append("A database metadata query failed. Driver details were suppressed.")
            financial["unverified"].append("Financial inspection stopped after a database metadata error.")
            storage["unverified"].append("Storage inspection stopped after a database metadata error.")
            return
        try:
            functions = connection.execute(FINANCIAL_FUNCTIONS).fetchall()
            financial["function_signatures"] = functions
            for function in functions:
                if not function["present"] or not function["result_matches"] or not function["executable"]:
                    financial["problems"].append(
                        f"Required financial function missing, incompatible, or not executable: {function['signature']}."
                    )
            check_trigger_definitions(connection, financial)
            check_financial_runtime(connection.execute(FINANCIAL_RUNTIME).fetchone(),
                                    connection.execute(SEAL_POLICIES).fetchall(), financial)
        except driver_error:
            financial["unverified"].append("A financial metadata query failed. Driver details were suppressed.")
            storage["unverified"].append("Storage inspection stopped after a financial metadata error.")
            return
        storage_access["policies"] = [policy for policy in database["policies"] if policy["schema"] == "storage"]
        storage_access["objects_relation"] = relations.get(("storage", "objects"))
        if storage_access["objects_relation"] is None:
            storage_access["unverified"].append("The storage.objects relation is not visible in the catalog.")
        if not any(policy["table_name"] == "objects" for policy in storage_access["policies"]):
            storage_access["unverified"].append("No storage.objects policies were observed in the catalog.")
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
    financial = section()
    storage = section()
    storage_access = section()
    storage_access["unverified"].append("SQL metadata cannot verify Storage API upload, download, or deletion.")
    with connect(**parameters, autocommit=True, row_factory=dict_row) as connection:
        del parameters
        inspect_target(connection, args, schema, database, financial, storage, storage_access, Error)
    overall = aggregate(database, financial, storage, storage_access)
    print(json.dumps(dict(
        status=overall, database=database, financial_safeguards=financial,
        storage=dict(bucket_metadata=storage, access=storage_access),
        limitations=[
            "PASS covers inspected metadata only. It is not deployment approval.",
            "Catalog checks do not prove policy behavior, function bodies, or effective access to individual rows.",
            "Runtime settings describe this login and endpoint; application-level session overrides are not verified.",
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
