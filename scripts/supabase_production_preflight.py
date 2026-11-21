"""Opt-in Production preflight. No application startup or schema changes."""

import argparse
import ast
from collections import defaultdict
import getpass
import json
import os
from pathlib import Path
import re
import sys
import warnings


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = "svziludszaidqtkjrtfr"
DEVELOPMENT = "pxiwoowpoewxvnsjwfdw"
FEATURE_TABLES = {
    "Authentication and profiles": {"users"},
    "Provider verification": {"role_applications", "credentials", "role_application_history"},
    "Booking, meetings and rescheduling": {
        "appointments", "mentor_availability", "mentor_unavailable_dates", "reschedule_proposals",
    },
    "Messaging and dispute review": {"messages", "reports"},
    "Attendance and gamification": {"attendance_events", "xp_events"},
    "Payment approval and accounting": {
        "payments", "payment_events", "ledger_accounts", "ledger_transactions", "ledger_lines", "admin_audit_events",
    },
    "Discord integration": {"discord_accounts", "discord_rooms", "discord_room_events"},
}
RLS_TABLES = {
    "role_application_history", "payment_events", "attendance_events", "xp_events",
    "ledger_accounts", "ledger_transactions", "ledger_lines", "admin_audit_events",
    "discord_accounts", "discord_rooms", "discord_room_events", "reschedule_proposals",
}
FUNCTIONS = {"phase2_approval_ready", "phase2_allocation_complete", "seal_ledger_transaction"}
TRIGGERS = {
    ("payments", "phase2_payment_marker"): ("phase2_payment_marker", 23),
    ("payments", "phase2_require_allocation"): ("phase2_require_allocation", 17),
    ("appointments", "phase2_appointment_scope"): ("phase2_appointment_scope", 19),
    ("admin_audit_events", "phase2_audit_link"): ("phase2_audit_link", 7),
    ("ledger_transactions", "phase2_check_allocation"): ("phase2_check_allocation", 17),
    ("ledger_transactions", "ledger_transaction_seal_guard"): ("guard_ledger_transaction", 23),
    ("ledger_transactions", "ledger_transaction_requires_seal"): ("require_sealed_ledger_transaction", 5),
    ("ledger_transactions", "ledger_transaction_compensation_decision"): ("require_compensation_decision", 19),
    ("ledger_lines", "ledger_line_insert_guard"): ("guard_ledger_line_insert", 7),
    ("admin_audit_events", "admin_audit_related_target"): ("require_related_audit_target", 7),
}
for _table in ("ledger_accounts", "ledger_transactions", "ledger_lines", "admin_audit_events"):
    TRIGGERS[_table, f"{_table}_no_mutation"] = (
        "prevent_financial_history_mutation", 11 if _table == "ledger_transactions" else 27)
    TRIGGERS[_table, f"{_table}_no_truncate"] = ("prevent_financial_history_mutation", 34)
for _table, _function in (
    ("payment_events", "prevent_payment_event_mutation"),
    ("role_application_history", "prevent_role_application_history_mutation"),
):
    TRIGGERS[_table, f"{_table}_no_mutation"] = (_function, 27)
    TRIGGERS[_table, f"{_table}_no_truncate"] = (_function, 34)


class Blocked(Exception):
    """Fixed-text reportable error."""


class SafeArguments(argparse.ArgumentParser):
    def error(self, _message):
        raise Blocked("Invalid operator arguments. Use only non-secret endpoint, role and bucket identifiers.")


def migration_graph():
    revisions = {}
    for path in sorted((ROOT / "migrations" / "versions").glob("*.py")):
        fields = {}
        for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                    fields[target.id] = ast.literal_eval(node.value)
        revision, parent = fields["revision"], fields["down_revision"]
        if revision in revisions:
            raise Blocked("Duplicate local migration revision.")
        revisions[revision] = () if parent is None else (parent,) if isinstance(parent, str) else tuple(parent)
    parents = {p for group in revisions.values() for p in group}
    heads = set(revisions) - parents
    if len(heads) != 1 or not parents <= revisions.keys():
        raise Blocked("Local migration graph has missing parents or does not have one head.")
    head = next(iter(heads))
    visited, active = set(), set()

    def visit(revision):
        if revision in active:
            raise Blocked("Local migration graph contains a cycle.")
        if revision not in visited:
            active.add(revision)
            for parent in revisions[revision]:
                visit(parent)
            active.remove(revision)
            visited.add(revision)

    visit(head)
    if visited != set(revisions):
        raise Blocked("Local migration graph is disconnected.")
    return head, revisions


def connection_parameters(raw, args, parse):
    raw = re.sub(r"^postgres(?:ql\+psycopg2?)?://", "postgresql://", raw, count=1)
    if not raw.startswith("postgresql://"):
        raise Blocked("An explicit PostgreSQL URL is required.")
    try:
        parameters = parse(raw)
    except Exception:
        raise Blocked("PostgreSQL URL is malformed.") from None
    if set(parameters) - {"host", "port", "dbname", "user", "password", "sslmode"}:
        raise Blocked("Additional connection options are not allowed.")
    host, user = parameters.get("host", "").lower(), parameters.get("user", "")
    if DEVELOPMENT in host or DEVELOPMENT in user:
        raise Blocked("Development project rejected.")
    direct = host == f"db.{PRODUCTION}.supabase.co" and user == args.expected_runtime_role
    pooled = re.fullmatch(r"[a-z0-9-]+\.pooler\.supabase\.com", host) and (
        user == f"{args.expected_runtime_role}.{PRODUCTION}")
    if not (direct or pooled):
        raise Blocked("Production project identity could not be established from host and login.")
    if (host != args.expected_host.lower() or parameters.get("port", "5432") != str(args.expected_port)
            or parameters.get("dbname") != "postgres" or not parameters.get("password")):
        raise Blocked("URL differs from the approved endpoint or database.")
    return dict(host=host, port=args.expected_port, dbname="postgres", user=user,
                password=parameters["password"], sslmode="verify-full", gssencmode="disable", connect_timeout=10)


IDENTITY = """
SELECT pg_catalog.current_database() AS database, current_user AS role, session_user AS session_role,
 pg_catalog.current_schema() AS schema, pg_catalog.current_setting('transaction_read_only') AS read_only,
 pg_catalog.current_setting('server_version_num') AS version, r.rolsuper AS superuser, r.rolbypassrls AS bypass_rls,
 ARRAY(SELECT g.rolname FROM pg_catalog.pg_roles g WHERE g.oid<>r.oid
       AND pg_catalog.pg_has_role(r.oid,g.oid,'USAGE') ORDER BY g.rolname) AS inherited_roles
FROM pg_catalog.pg_roles r WHERE r.rolname=current_user
"""
RELATIONS = """
SELECT n.nspname AS schema,c.relname AS name,c.relkind AS kind,
 pg_catalog.pg_get_userbyid(c.relowner) AS owner,pg_catalog.pg_has_role(current_user,c.relowner,'USAGE') AS owner_access,
 c.relrowsecurity AS rls,c.relforcerowsecurity AS forced,
 CASE WHEN c.relkind IN ('r','p') THEN pg_catalog.row_security_active(c.oid) END AS rls_active,
 pg_catalog.has_table_privilege(c.oid,'SELECT') AS readable,pg_catalog.has_table_privilege(c.oid,'INSERT') AS insertable,
 pg_catalog.has_table_privilege(c.oid,'UPDATE') AS updatable,pg_catalog.has_table_privilege(c.oid,'DELETE') AS deletable,
 ARRAY(SELECT r.rolname FROM pg_catalog.pg_roles r WHERE r.rolname IN ('anon','authenticated')
       AND pg_catalog.has_schema_privilege(r.oid,n.oid,'USAGE')
       AND (pg_catalog.has_any_column_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE')
            OR pg_catalog.has_table_privilege(r.oid,c.oid,'DELETE,TRUNCATE'))) AS browser_roles
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p','v','m','f')
 AND (n.nspname='public' OR (n.nspname='storage' AND c.relname IN ('buckets','objects')))
ORDER BY n.nspname,c.relname
"""
COLUMNS = """
SELECT n.nspname AS schema,c.relname AS table_name,a.attname AS name,
 pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,NOT a.attnotnull AS nullable,
 tn.nspname AS type_schema,t.typname AS internal_type,a.attgenerated<>'' AS generated,
 a.atthasdef AS has_default,a.attidentity<>'' AS identity,
 pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS default_expression
FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
WHERE a.attnum>0 AND NOT a.attisdropped AND ((n.nspname='public' AND c.relname=ANY(%s))
 OR (n.nspname='storage' AND c.relname='buckets'))
"""
CONSTRAINTS = """
SELECT t.relname AS table_name,c.conname AS name,c.contype AS kind,c.convalidated AS validated,
 ARRAY(SELECT a.attname FROM pg_catalog.unnest(c.conkey) WITH ORDINALITY k(num,pos)
       JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num ORDER BY k.pos) AS columns,
 nf.nspname AS target_schema,f.relname AS target_table,c.confdeltype AS on_delete,
 ARRAY(SELECT a.attname FROM pg_catalog.unnest(c.confkey) WITH ORDINALITY k(num,pos)
       JOIN pg_catalog.pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num ORDER BY k.pos) AS target_columns
FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
LEFT JOIN pg_catalog.pg_class f ON f.oid=c.confrelid
LEFT JOIN pg_catalog.pg_namespace nf ON nf.oid=f.relnamespace
WHERE n.nspname='public' AND t.relname=ANY(%s)
ORDER BY t.relname,c.conname
"""
INDEXES = """
SELECT t.relname AS table_name,i.relname AS name,x.indisunique AS unique_index,
 x.indisvalid AND x.indisready AS valid,x.indpred IS NOT NULL AS partial,
 x.indexprs IS NOT NULL AS expression,x.indnkeyatts AS keys,
 ARRAY(SELECT a.attname FROM pg_catalog.unnest(x.indkey) WITH ORDINALITY k(num,pos)
       JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.num
       WHERE k.pos<=x.indnkeyatts ORDER BY k.pos) AS columns
FROM pg_catalog.pg_index x JOIN pg_catalog.pg_class t ON t.oid=x.indrelid
JOIN pg_catalog.pg_class i ON i.oid=x.indexrelid JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
WHERE n.nspname='public' AND t.relname=ANY(%s)
ORDER BY t.relname,i.relname
"""
POLICIES = """
SELECT n.nspname AS schema,c.relname AS table_name,p.polname AS name,p.polcmd AS command,p.polpermissive AS permissive,
 ARRAY(SELECT CASE WHEN id=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(id)::text END
       FROM pg_catalog.unnest(p.polroles) id) AS roles,
 EXISTS(SELECT 1 FROM pg_catalog.unnest(p.polroles) id
        WHERE CASE WHEN id=0 THEN true ELSE pg_catalog.pg_has_role(current_user,id,'USAGE') END) AS applies
FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c ON c.oid=p.polrelid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE (n.nspname='public' AND c.relname=ANY(%s))
 OR (n.nspname='storage' AND c.relname IN ('buckets','objects'))
"""
TRIGGER_SQL = """
SELECT c.relname AS table_name,t.tgname AS name,t.tgenabled AS enabled,t.tgtype AS events,
 t.tgdeferrable AS deferrable,t.tginitdeferred AS initially_deferred,t.tgqual IS NOT NULL AS conditional,
 p.proname AS function,nf.nspname AS function_schema
FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
JOIN pg_catalog.pg_namespace nf ON nf.oid=p.pronamespace
WHERE n.nspname='public' AND c.relname=ANY(%s) AND NOT t.tgisinternal
"""
FUNCTION_SQL = """
SELECT p.proname AS name,p.pronargs AS arguments,p.prosecdef AS security_definer,
 pg_catalog.pg_get_userbyid(p.proowner) AS owner,pg_catalog.has_function_privilege(p.oid,'EXECUTE') AS executable
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.prokind='f'
"""
SEQUENCES = """
SELECT s.relname AS name,t.relname AS table_name,pg_catalog.has_sequence_privilege(s.oid,'USAGE') AS usage,
 pg_catalog.has_sequence_privilege(s.oid,'UPDATE') AS updatable
FROM pg_catalog.pg_class s JOIN pg_catalog.pg_namespace n ON n.oid=s.relnamespace
JOIN pg_catalog.pg_depend d ON d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid=s.oid AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refobjsubid>0 AND d.deptype IN ('a','i')
JOIN pg_catalog.pg_class t ON t.oid=d.refobjid
WHERE n.nspname='public' AND s.relkind='S' AND t.relname=ANY(%s)
"""
MIGRATION_ROWS = "SELECT version_num FROM ONLY public.alembic_version ORDER BY version_num LIMIT 32"
BUCKET_ROWS = "SELECT name, public, file_size_limit, allowed_mime_types FROM ONLY storage.buckets ORDER BY name LIMIT 201"


def local_schema():
    # Declaration imports only. No create_app, config, or database engine.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    from models import db
    from sqlalchemy.dialects import postgresql
    from audit_database_security import REQUIRED_ACCESS
    if set(db.metadata.tables) != set(REQUIRED_ACCESS) or set(db.metadata.tables) != set().union(*FEATURE_TABLES.values()):
        raise Blocked("Local model and runtime access inventories disagree.")
    return db.metadata.tables, postgresql.dialect(), REQUIRED_ACCESS


def table_rows(connection, sql, args=()):
    result = defaultdict(list)
    for row in connection.execute(sql, args).fetchall():
        result[row["table_name"]].append(row)
    return result


def type_key(value):
    return re.sub(r"\s+", " ", value.lower()).replace("character varying", "varchar").replace(
        "timestamp without time zone", "timestamp").replace(", ", ",").strip()


def metadata_readable(relation, columns, required):
    # Avoid views, inheritance, custom types, generated values and RLS policy execution.
    return bool(relation and relation["kind"] == "r" and relation["readable"] and not relation["rls_active"]
                and all(c in columns and columns[c]["type_schema"] == "pg_catalog"
                        and columns[c]["internal_type"] in types and not columns[c]["generated"]
                        for c, types in required.items()))


def verify_identity(identity, args):
    if not identity or identity["read_only"] != "on":
        raise Blocked("Transaction read-only mode could not be confirmed.")
    if (identity["database"] != "postgres" or identity["schema"] != "public"
            or identity["role"] != args.expected_runtime_role or identity["session_role"] != args.expected_runtime_role):
        raise Blocked("Connected database, default schema, or SQL role differs from approved identity.")


def compare_table(table, actual, columns, constraints, indexes, policies, dialect, access, report):
    from sqlalchemy import CheckConstraint, ForeignKeyConstraint, PrimaryKeyConstraint, UniqueConstraint

    name = table.name
    expected = {c.name: {"type": c.type.compile(dialect=dialect), "nullable": c.nullable} for c in table.columns}
    details = {"expected_columns": expected, "catalog_constraints": constraints, "catalog_indexes": indexes}
    report["application_schema"][name] = details
    if not actual or actual["kind"] not in ("r", "p"):
        details["status"] = "MISSING" if not actual else "INCOMPATIBLE RELATION KIND"
        report["problems"].append(f"{name}: expected an application table.")
        return
    details.update(status="PRESENT", privileges=actual, columns=[], constraints=[], indexes=[],
                   unexpected_columns=sorted(set(columns) - set(expected)))
    for col in table.columns:
        found = columns.get(col.name)
        if not found or type_key(found["type"]) != type_key(expected[col.name]["type"]) or found["nullable"] != col.nullable:
            status = "MISSING" if not found else "INCOMPATIBLE"
            report["problems"].append(f"{name}.{col.name}: {status.lower()} column.")
        elif col is table.autoincrement_column:
            status = "MATCH" if found["identity"] or str(found["default_expression"]).startswith("nextval(") else "REVIEW DEFAULT"
        elif bool(col.server_default) == found["has_default"] and col.server_default is None:
            status = "MATCH"
        else:
            status = "REVIEW DEFAULT"
        details["columns"].append(dict(name=col.name, status=status,
                                      actual_type=found["type"] if found else None,
                                      actual_nullable=found["nullable"] if found else None,
                                      has_default=found["has_default"] if found else None))
    kinds = {PrimaryKeyConstraint: "p", UniqueConstraint: "u", ForeignKeyConstraint: "f", CheckConstraint: "c"}
    for c in table.constraints:
        kind = kinds.get(type(c))
        if not kind:
            continue
        fields = [col.name for col in c.columns]
        candidates = [r for r in constraints if r["kind"] == kind and r["validated"]
                      and (r["name"] == c.name if kind == "c" else list(r["columns"]) == fields)]
        item = dict(name=c.name, kind=kind, columns=fields)
        if kind == "f":
            target = c.elements[0].column.table.name
            target_cols = [e.column.name for e in c.elements]
            delete = {"CASCADE": "c", "RESTRICT": "r", "SET NULL": "n", "SET DEFAULT": "d"}
            code = delete.get((c.ondelete or "").upper(), "a")
            item.update(target=target, target_columns=target_cols, on_delete=code)
            candidates = [r for r in candidates if r["target_schema"] == "public" and r["target_table"] == target
                          and list(r["target_columns"]) == target_cols and r["on_delete"] == code]
        item["status"] = ("REVIEW EXPRESSION" if kind == "c" else "MATCH") if candidates else "MISSING OR INCOMPATIBLE"
        details["constraints"].append(item)
        if not candidates:
            report["problems"].append(f"{name}: missing or incompatible expected {kind} constraint.")
    by_name = {r["name"]: r for r in indexes}
    for index in sorted(table.indexes, key=lambda i: i.name):
        found = by_name.get(index.name)
        partial = index.dialect_options["postgresql"].get("where") is not None
        expression = any(not hasattr(x, "table") for x in index.expressions)
        fields = [c.name for c in index.columns]
        match = (found and found["valid"] and found["unique_index"] == bool(index.unique)
                 and found["partial"] == partial and found["expression"] == expression
                 and found["keys"] == len(index.expressions) and (expression or list(found["columns"]) == fields))
        details["indexes"].append(dict(name=index.name, columns=fields, partial=partial, expression=expression,
                                       status=("REVIEW PREDICATE/EXPRESSION" if partial or expression else "MATCH")
                                       if match else "MISSING OR INCOMPATIBLE"))
        if not match:
            report["problems"].append(f"{name}: expected index {index.name} missing or incompatible.")
    operations = {"SELECT": "readable", "INSERT": "insertable", "UPDATE": "updatable", "DELETE": "deletable"}
    details["runtime_table_grants_missing"] = [op for op in access.split() if not actual[operations[op]]]
    if details["runtime_table_grants_missing"]:
        report["unverified"].append(f"{name}: review missing table grants and any compensating column grants.")
    if name in RLS_TABLES and (not actual["rls"] or actual["browser_roles"]):
        report["problems"].append(f"{name}: expected RLS or browser-role grant protection differs.")
    if actual["rls_active"]:
        applicable = {p["command"] for p in policies if p["applies"] and p["permissive"]}
        commands = {"SELECT": "r", "INSERT": "a", "UPDATE": "w", "DELETE": "d"}
        if "*" not in applicable and any(commands[op] not in applicable for op in access.split()):
            report["problems"].append(f"{name}: no applicable permissive RLS policy for one or more runtime operations.")


def inspect(connection, args, identity, head, revisions, tables, dialect, access, report):
    report["identity"] = dict(identity, project_reference=PRODUCTION, verified_tls_endpoint=args.expected_host)
    if identity["superuser"] or identity["bypass_rls"]:
        report["problems"].append("Runtime role can bypass RLS.")
    names = sorted(tables)
    relations = {(r["schema"], r["name"]): r for r in connection.execute(RELATIONS).fetchall()}
    columns = defaultdict(dict)
    for row in connection.execute(COLUMNS, (names + ["alembic_version"],)).fetchall():
        columns[(row["schema"], row["table_name"])][row["name"]] = row
    version = ("public", "alembic_version")
    revision_rows = []
    if version not in relations:
        state = "MISSING"
    elif not metadata_readable(relations[version], columns[version], {"version_num": {"varchar", "text"}}):
        state = "UNVERIFIED"
        report["unverified"].append("Cannot safely read Alembic revision metadata as this role.")
    else:
        revision_rows = [r["version_num"] for r in connection.execute(MIGRATION_ROWS).fetchall()]
        if revision_rows == [head]:
            state = "CURRENT"
        elif len(revision_rows) == 1 and revision_rows[0] in revisions:
            state = "BEHIND"
        elif len(revision_rows) == 1:
            state = "UNKNOWN REVISION (ahead or divergent)"
        else:
            state = "INCONSISTENT (empty or multiple revisions)"
    report["migration"] = dict(status=state, expected_head=head, observed_count_up_to_limit=len(revision_rows),
                               observed=[r if r in revisions else "<unknown revision>" for r in revision_rows])
    if state not in ("CURRENT", "UNVERIFIED"):
        report["problems"].append("Production revision requires a reviewed reconciliation decision. Do not stamp.")
    report["unexpected_public_relations"] = sorted(name for schema, name in relations
                                                    if schema == "public" and name not in tables and name != "alembic_version")
    constraints, indexes = table_rows(connection, CONSTRAINTS, (names,)), table_rows(connection, INDEXES, (names,))
    policies = connection.execute(POLICIES, (names,)).fetchall()
    report["policies"] = policies
    report["runtime_sequences"] = connection.execute(SEQUENCES, (names,)).fetchall()
    for sequence in report["runtime_sequences"]:
        if "INSERT" in access[sequence["table_name"]].split() and not (sequence["usage"] or sequence["updatable"]):
            report["review"].append(f"{sequence['name']}: verify sequence access for runtime inserts.")
    for name in names:
        key = ("public", name)
        applicable = [p for p in policies if (p["schema"], p["table_name"]) == key]
        compare_table(tables[name], relations.get(key), columns[key], constraints[name],
                      indexes[name], applicable, dialect, access[name], report)
    triggers = connection.execute(TRIGGER_SQL, (names,)).fetchall()
    report["triggers"] = triggers
    by_key = {(r["table_name"], r["name"]): r for r in triggers}
    for key, (function, events) in sorted(TRIGGERS.items()):
        t = by_key.get(key)
        if (not t or t["enabled"] not in ("O", "A") or t["function_schema"] != "public"
                or t["function"] != function or t["events"] != events):
            report["problems"].append("Trigger missing or incompatible: " + ".".join(key))
        elif key[1] in ("phase2_require_allocation", "ledger_transaction_requires_seal") and not (
            t["deferrable"] and t["initially_deferred"]
        ):
            report["problems"].append("Expected deferred trigger differs: " + ".".join(key))
    report["functions"] = connection.execute(FUNCTION_SQL).fetchall()
    for name in sorted(FUNCTIONS - {r["name"] for r in report["functions"]}):
        report["problems"].append(f"Expected function missing: {name}.")
    inspect_storage(connection, args.receipt_bucket, relations, columns, report)


def inspect_storage(connection, receipt_bucket, relations, columns, report):
    storage = {name: relations.get(("storage", name)) for name in ("objects", "buckets")}
    report["storage"] = {"relations": storage}
    required = {"name": {"text", "varchar"}, "public": {"bool"},
                "file_size_limit": {"int8", "int4"}, "allowed_mime_types": {"_text"}}
    expected = {
        "avatars": (True, 2 * 1024**2, {"image/jpeg", "image/png", "image/webp"}),
        "credentials": (False, 5 * 1024**2, {"application/pdf", "image/jpeg", "image/png"}),
        receipt_bucket: (False, 5 * 1024**2, {"image/jpeg", "image/png"}),
    }
    report["storage"]["expected"] = {name: dict(public=public, app_limit=size, mime=sorted(mime))
                                      for name, (public, size, mime) in expected.items()}
    if not metadata_readable(storage["buckets"], columns[("storage", "buckets")], required):
        report["unverified"].append("Storage bucket metadata is not safely readable as this role.")
        return
    rows = connection.execute(BUCKET_ROWS).fetchall()
    if len(rows) > 200:
        report["unverified"].append("Storage bucket inventory exceeded its cap.")
        return
    report["storage"]["buckets"] = rows
    by_name = {row["name"]: row for row in rows}
    for name, (public, size, mime) in expected.items():
        row = by_name.get(name)
        if not row or row["public"] is not public:
            report["problems"].append(f"Storage bucket missing or privacy differs: {name}.")
        elif row["file_size_limit"] is not None and row["file_size_limit"] < size:
            report["problems"].append(f"Storage bucket limit below the application limit: {name}.")
        elif row["allowed_mime_types"] is not None and not mime <= set(row["allowed_mime_types"]):
            report["problems"].append(f"Storage bucket MIME policy does not explicitly include all application types: {name}.")
        elif row["file_size_limit"] != size or set(row["allowed_mime_types"] or []) != mime:
            report["review"].append(f"Storage size or MIME policy requires review: {name}.")


def main():
    parser = SafeArguments(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-port", type=int, required=True)
    parser.add_argument("--expected-runtime-role", required=True)
    parser.add_argument("--receipt-bucket", default="payment-receipts")
    args = parser.parse_args()
    if (not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", args.expected_host)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", args.expected_runtime_role)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", args.receipt_bucket)
            or args.receipt_bucket in ("avatars", "credentials")
            or not 1 <= args.expected_port <= 65535 or not sys.stdin.isatty() or not sys.stderr.isatty()):
        raise Blocked("An interactive terminal and valid non-secret endpoint, role and bucket identifiers are required.")
    if any(key.upper().startswith("PG") and key.upper() != "PGSSLROOTCERT" for key in os.environ):
        raise Blocked("Ambient libpq overrides are present. Use an approved clean terminal.")
    from psycopg import Error, connect
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row

    head, revisions = migration_graph()
    tables, dialect, access = local_schema()
    print("Expected Production project:", PRODUCTION)
    print("Approved endpoint:", args.expected_host, args.expected_port, "postgres/public")
    print("Expected runtime SQL role:", args.expected_runtime_role)
    if input("Type APPROVE PRODUCTION READ ONLY to connect: ") != "APPROVE PRODUCTION READ ONLY":
        raise Blocked("Production consent was not supplied. No connection made.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        parameters = connection_parameters(getpass.getpass("Production DATABASE_URL (hidden): "), args, conninfo_to_dict)
    report = dict(problems=[], unverified=[], review=[], application_schema={}, expected_head=head,
                  tables_by_feature={feature: sorted(names) for feature, names in FEATURE_TABLES.items()})
    stage = "connection"
    try:
        with connect(**parameters, autocommit=True, row_factory=dict_row) as connection:
            del parameters
            stage = "read-only transaction"
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                identity = connection.execute(IDENTITY).fetchone()
                verify_identity(identity, args)
                stage = "catalog or approved metadata"
                inspect(connection, args, identity, head, revisions, tables, dialect, access, report)
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except Error:
                    pass  # The connection context closes it. Preserve the original error.
                raise
            else:
                connection.execute("ROLLBACK")
    except Error as exc:
        if stage != "catalog or approved metadata":
            raise Blocked(f"Unable to establish {stage}; driver details suppressed.") from None
        state = exc.sqlstate
        code = state if state and re.fullmatch(r"[0-9A-Z]{5}", state) else "unavailable"
        report["unverified"].append(f"Inspection stopped at {stage}, SQLSTATE {code}. No workaround attempted.")
    report["assessment"] = ("INCOMPLETE: PERMISSIONS OR METADATA UNAVAILABLE" if report["unverified"]
                            else "NOT READY" if report["problems"] else "READY FOR NEXT REVIEW")
    report["limits"] = [
        "Catalog indicators are not proofs of policy semantics, effective row access or application success.",
        "Review defaults, CHECK expressions, partial predicates, function bodies and trigger conditions privately.",
        "Compare receipt revision protections and financial runtime sequence, column and function grants before activation.",
        "No application rows, file contents, raw defaults, policy expressions, or function bodies are reported.",
    ]
    report["future_changes_requiring_approval"] = [
        "Plan any schema, grant, RLS or Storage correction separately after confirming observed mismatches.",
        "Verify Storage API access against isolated synthetic data separately.",
        "Keep financial approvals disabled until hosted safeguards are independently reviewed.",
    ]
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["assessment"] == "READY FOR NEXT REVIEW" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Blocked as exc:
        print("BLOCKED: READ-ONLY CHECK FAILED")
        print(str(exc))
        sys.exit(2)
    except (Exception, KeyboardInterrupt):
        print("BLOCKED: READ-ONLY CHECK FAILED")
        print("Preflight stopped; details suppressed to protect credentials and metadata.")
        sys.exit(2)
