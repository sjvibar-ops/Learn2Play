"""Catalog queries and reviewed expectations for Development at d2b7a4e9310c."""

import hashlib
import re

BASE_TABLES = {
    "alembic_version", "appointments", "credentials", "mentor_availability",
    "mentor_unavailable_dates", "messages", "reports", "role_applications", "users", "xp_events",
}
ID_TABLES = BASE_TABLES - {"alembic_version"}
APPOINTMENT = (
    ("id", "integer", True), ("provider_id", "integer", True), ("student_id", "integer", True),
    ("role", "character varying(20)", True), ("topic", "character varying(120)", True),
    ("start_time", "timestamp without time zone", True), ("duration_minutes", "integer", True),
    ("status", "character varying(20)", True), ("price", "numeric(10,2)", False),
    ("currency", "character varying(10)", True), ("location_type", "character varying(50)", False),
    ("meeting_link", "character varying(255)", False), ("notes", "text", False),
    ("meeting_room", "character varying(80)", False),
    ("created_at", "timestamp without time zone", True), ("updated_at", "timestamp without time zone", True),
    ("provider_confirmed_at", "timestamp without time zone", False),
    ("student_confirmed_at", "timestamp without time zone", False),
    ("attendance_disputed_at", "timestamp without time zone", False),
    ("attendance_disputed_by_id", "integer", False),
)
PENDING_COLUMNS = {
    "attendance_closed_at", "attendance_resolution", "attendance_resolution_reason",
    "attendance_resolved_at", "attendance_resolved_by_id", "ended_at", "ended_by_id",
    "ending_reason", "payment_required",
}
PENDING_PAYMENTS = {"payments", "payment_events"}

# Compare semantics and index key order rather than relying on generated names.
PRIMARY = {(t, "p", ("id",), None, ()) for t in ID_TABLES} | {
    ("alembic_version", "p", ("version_num",), None, ()),
}
UNIQUE = {
    ("appointments", "meeting_room"), ("mentor_availability", "mentor_id weekday start_time end_time"),
    ("mentor_unavailable_dates", "mentor_id unavailable_date"),
    ("role_applications", "user_id role"), ("xp_events", "appointment_id"),
}
REFERENCES = {
    "appointments": "provider_id student_id attendance_disputed_by_id",
    "credentials": "user_id", "mentor_availability": "mentor_id",
    "mentor_unavailable_dates": "mentor_id", "role_applications": "user_id",
    "messages": "sender_id recipient_id appointment_id",
    "reports": "reporter_id reported_id reviewed_by appointment_id",
    "xp_events": "user_id appointment_id",
}
CONSTRAINTS = PRIMARY | {(t, "u", tuple(cols.split()), None, ()) for t, cols in UNIQUE} | {
    (table, "f", (column,), "appointments" if column == "appointment_id" else "users", ("id",))
    for table, columns in REFERENCES.items() for column in columns.split()
}
CHECK_EXPRESSIONS = {
    "ck_xp_events_positive_points": "points>0",
    "ck_xp_events_path": "path=ANYARRAY['academic','gaming']",
}
EXPLICIT_INDEXES = {
    "users": ("email",),
    "appointments": ("provider_id", "start_time", "student_id"),
    "credentials": ("user_id",),
    "mentor_availability": ("mentor_id",), "mentor_unavailable_dates": ("mentor_id",),
    "role_applications": ("user_id",),
    "messages": ("appointment_id", "recipient_id", "sender_id"),
    "reports": ("appointment_id", "reported_id", "reporter_id"),
}
INDEXES = {(t, cols, True) for t, kind, cols, _, _ in CONSTRAINTS if kind in {"p", "u"}} | {
    (t, (column,), t == "users") for t, columns in EXPLICIT_INDEXES.items() for column in columns
} | {("xp_events", ("user_id", "created_at"), False)}

PRINCIPALS = ("postgres", "anon", "authenticated", "service_role")
ROLE_SCOPE = """
SELECT oid, rolname::text AS principal FROM pg_catalog.pg_roles
WHERE rolname IN ('anon','authenticated','service_role','postgres') OR rolname = current_user
UNION ALL SELECT 0::oid, 'PUBLIC'
"""
BROWSER_PRIVILEGES = """
SELECT r.principal, c.relname AS relation, c.relkind, a.attname AS column_name,
       has_column_privilege(r.oid,c.oid,a.attnum,'SELECT') AS can_read,
       has_column_privilege(r.oid,c.oid,a.attnum,'INSERT') AS can_insert,
       has_column_privilege(r.oid,c.oid,a.attnum,'UPDATE') AS can_update,
       has_column_privilege(r.oid,c.oid,a.attnum,'REFERENCES') AS can_reference
FROM (""" + ROLE_SCOPE + """) r
CROSS JOIN pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f')
AND a.attnum>0 AND NOT a.attisdropped
ORDER BY r.principal,c.relname,a.attnum
"""
CATALOG = {
    "columns": """
SELECT c.relname AS table_name,a.attnum AS position,a.attname AS name,
       format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull AS not_null,
       pg_get_expr(d.adbin,d.adrelid) AS default_expression,
       a.attidentity AS identity_kind,a.attgenerated AS generated_kind
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
WHERE n.nspname='public' AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
ORDER BY c.relname,a.attnum
""",
    "constraints": """
SELECT c.relname AS table_name,k.conname AS name,k.contype AS kind,k.convalidated AS valid,
       k.condeferrable AS deferrable,k.condeferred AS deferred,
       k.confupdtype AS on_update,k.confdeltype AS on_delete,k.confmatchtype AS match_type,
       ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY AS u(num,ord)
         JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum=u.num ORDER BY u.ord) AS columns,
       CASE WHEN f.oid IS NOT NULL THEN fn.nspname||'.'||f.relname END AS target,
       ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY AS u(num,ord)
         JOIN pg_catalog.pg_attribute a ON a.attrelid=f.oid AND a.attnum=u.num ORDER BY u.ord) AS target_columns,
       pg_get_expr(k.conbin,k.conrelid) AS expression
FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_catalog.pg_class f ON f.oid=k.confrelid
LEFT JOIN pg_catalog.pg_namespace fn ON fn.oid=f.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname,k.conname
""",
    "indexes": """
SELECT c.relname AS table_name,x.relname AS name,i.indisunique AS unique,i.indisvalid AS valid,
       i.indisready AS ready,i.indislive AS live,i.indimmediate AS immediate,
       am.amname AS method,i.indpred IS NOT NULL AS partial,
       coalesce((to_jsonb(i)->>'indnullsnotdistinct')::boolean,false) AS nulls_not_distinct,
       ARRAY(SELECT pg_get_indexdef(i.indexrelid,k,true) FROM generate_series(1,i.indnatts::int) k) AS columns
FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indrelid
JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid JOIN pg_catalog.pg_am am ON am.oid=x.relam
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname,x.relname
""",
    "triggers": """
SELECT c.relname AS table_name,t.tgname AS name,t.tgenabled AS enabled,
       pg_get_triggerdef(t.oid) AS definition
FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname
""",
    "schema_privileges": """
SELECT r.principal,pg_get_userbyid(n.nspowner) AS owner,
       has_schema_privilege(r.oid,n.oid,'USAGE') AS can_use,
       has_schema_privilege(r.oid,n.oid,'CREATE') AS can_create
FROM (""" + ROLE_SCOPE + """) r CROSS JOIN pg_catalog.pg_namespace n
WHERE n.nspname='public' ORDER BY r.principal
""",
    "authority": """
SELECT c.relname AS relation,c.relkind,pg_get_userbyid(c.relowner) AS owner,
       pg_has_role(current_user,c.relowner,'USAGE') AS owner_authority,
       CASE WHEN c.relkind IN ('r','p') THEN has_table_privilege(c.oid,'REFERENCES') END AS can_reference,
       CASE WHEN c.relkind IN ('r','p') THEN has_table_privilege(c.oid,'TRIGGER') END AS can_trigger,
       CASE WHEN c.relkind IN ('r','p') THEN ARRAY(
          SELECT privilege FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'])
          AS options(privilege) WHERE has_table_privilege(c.oid,privilege||' WITH GRANT OPTION'))
       WHEN c.relkind='S' THEN ARRAY(
          SELECT privilege FROM unnest(ARRAY['USAGE','SELECT','UPDATE'])
          AS options(privilege) WHERE has_sequence_privilege(c.oid,privilege||' WITH GRANT OPTION'))
       END AS grant_options
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p','S') ORDER BY c.relname
""",
    "default_acl": """
SELECT pg_get_userbyid(d.defaclrole) AS creator,
       coalesce(n.nspname,'(global)') AS scope,d.defaclobjtype AS object_type,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee,
       pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM pg_catalog.pg_default_acl d LEFT JOIN pg_catalog.pg_namespace n ON n.oid=d.defaclnamespace
LEFT JOIN LATERAL aclexplode(d.defaclacl) a ON true
WHERE d.defaclnamespace=0 OR n.nspname='public'
ORDER BY creator,scope,object_type,grantee,a.privilege_type
""",
    "browser_columns": BROWSER_PRIVILEGES,
    "public_table_privileges": """
SELECT c.relname AS relation, ARRAY(
    SELECT privilege FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'])
    AS privileges(privilege) WHERE has_table_privilege(0::oid,c.oid,privilege)
) AS privileges
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname
""",
    "views": """
SELECT r.principal,c.relname AS name,c.relkind,pg_get_userbyid(c.relowner) AS owner,
       CASE WHEN c.relkind='v' THEN coalesce(c.reloptions @> ARRAY['security_invoker=true'],false) END AS security_invoker,
       has_table_privilege(r.oid,c.oid,'SELECT') AS can_read,
       has_table_privilege(r.oid,c.oid,'INSERT') AS can_insert,
       has_table_privilege(r.oid,c.oid,'UPDATE') AS can_update,
       has_table_privilege(r.oid,c.oid,'DELETE') AS can_delete
FROM (""" + ROLE_SCOPE + """) r
CROSS JOIN pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('v','m') ORDER BY c.relname,r.principal
""",
    "routines": """
SELECT r.principal,p.proname AS name,oidvectortypes(p.proargtypes) AS argument_types,p.prokind,
       pg_get_userbyid(p.proowner) AS owner,p.prosecdef AS security_definer,
       has_function_privilege(r.oid,p.oid,'EXECUTE') AS can_execute
FROM (""" + ROLE_SCOPE + """) r
CROSS JOIN pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' ORDER BY p.proname,p.oid,r.principal
""",
    "policy_definitions": """
SELECT tablename,policyname,qual,with_check FROM pg_catalog.pg_policies
WHERE schemaname='public' ORDER BY tablename,policyname
""",
    "sessions": """
SELECT pg_has_role(current_user,'pg_read_all_stats','USAGE') AS full_stats_privilege,
       count(*) AS other_sessions,count(*) FILTER (WHERE state='active') AS active,
       count(*) FILTER (WHERE state LIKE 'idle in transaction%') AS idle_in_transaction,
       count(*) FILTER (WHERE state IS NULL) AS state_unavailable,
       max(extract(epoch FROM statement_timestamp()-xact_start))::bigint AS oldest_visible_transaction_seconds,
       count(*) FILTER (WHERE cardinality(pg_blocking_pids(pid))>0) AS visibly_blocked
FROM pg_catalog.pg_stat_activity
WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()
""",
}
# Aggregate only. Never select account, receipt, transfer, or decision values.
DUPLICATES = """
SELECT count(*) AS duplicate_verified_groups FROM (
    SELECT 1 FROM public.payments WHERE status='VERIFIED' AND method IS NOT NULL
      AND transaction_reference IS NOT NULL
    GROUP BY lower(trim(method)),lower(trim(transaction_reference)) HAVING count(*)>1
) d
"""
DECISIONS = """
SELECT count(*) FILTER (WHERE reconciliation_state IN
    ('NO_FUNDS_RECEIVED','REFUND_PENDING','REFUNDED','RETAINED')) AS recorded_decisions
FROM public.payments
"""
RECONCILIATION = """
SELECT count(*) FILTER(WHERE a.status IN ('cancelled','rejected')
       AND (p.status<>'PENDING' OR p.receipt_path IS NOT NULL OR p.submitted_at IS NOT NULL
       OR p.reviewed_at IS NOT NULL OR p.submission_revision>0
       OR EXISTS (SELECT 1 FROM public.payment_events e WHERE e.payment_id=p.id))) AS backfill_candidates
FROM public.payments p JOIN public.appointments a ON a.id=p.appointment_id
"""


def status(value, state="verified", **extra):
    if isinstance(value, dict) and value.get("status") == "unavailable":
        return value
    return {"status": state, "rows": value, **extra}


def inventory(actual, expected, incompatible=()):
    missing = sorted(expected - actual, key=repr)
    unexpected = sorted(actual - expected, key=repr)
    return {
        "status": "mismatch" if missing or unexpected or incompatible else "verified",
        "missing": missing, "unexpected": unexpected, "incompatible": list(incompatible),
    }


def compare_columns(data):
    columns = [row for row in data if row["table_name"] == "appointments"]
    expected = {column[0]: (i, *column[1:]) for i, column in enumerate(APPOINTMENT, 1)}
    incompatible = []
    for row in columns:
        name = row["name"]
        if name not in expected:
            continue
        expected_default = "nextval('appointments_id_seq'::regclass)" if name == "id" else None
        default = row["default_expression"]
        if default == "nextval('public.appointments_id_seq'::regclass)":
            default = "nextval('appointments_id_seq'::regclass)"
        if ((row["position"], row["type"], row["not_null"]) != expected[name]
                or default != expected_default or row["identity_kind"] or row["generated_kind"]):
            incompatible.append(name)
    other = [row for row in data if (row["table_name"], row["name"]) == ("users", "leaderboard_opt_in")]
    if (len(other) != 1 or other[0]["type"] != "boolean" or not other[0]["not_null"]
            or other[0]["default_expression"] != "false"):
        incompatible.append("users.leaderboard_opt_in")
    return inventory({row["name"] for row in columns}, set(expected), incompatible)


def compare_constraints(data):
    actual, checks, incompatible = set(), set(), []
    for row in data:
        key = (row["table_name"], row["kind"], tuple(row["columns"]),
               row["target"], tuple(row["target_columns"]))
        if row["kind"] == "c" and row["table_name"] == "xp_events":
            checks.add(row["name"])
            expression = row["expression"] or ""
            # Permit PostgreSQL's parentheses and text/varchar coercions for these known checks.
            normalized = re.sub(
                r"'(?:''|[^'])*'|::(?:text|character varying)(?:\[\])?|\s+|[()]",
                lambda match: match[0] if match[0].startswith("'") else "",
                expression,
            )
            invalid = normalized != CHECK_EXPRESSIONS.get(row["name"])
        else:
            actual.add(key)
            invalid = row["kind"] == "f" and (
                row["on_update"], row["on_delete"], row["match_type"]
            ) != ("a", "a", "s")
        if invalid or not row["valid"] or row["deferrable"] or row["deferred"]:
            incompatible.append(row["name"])
        if row["table_name"] == "appointments" and key == (
            "appointments", "u", ("meeting_room",), None, ()
        ) and row["name"] != "uq_appointments_meeting_room":
            incompatible.append(row["name"])
    expected = {(t, k, cols, f"public.{target}" if target else None, refs)
                for t, k, cols, target, refs in CONSTRAINTS}
    if len(actual) + len(checks) != len(data):
        incompatible.append("duplicate constraint signatures")
    base = inventory(actual, expected, incompatible)
    check_result = inventory(checks, set(CHECK_EXPRESSIONS))
    if check_result["status"] == "mismatch":
        base["status"] = "mismatch"
        base["missing"].extend(check_result["missing"])
        base["unexpected"].extend(check_result["unexpected"])
    return base


def compare_indexes(data):
    actual = {(row["table_name"], tuple(row["columns"]), row["unique"]) for row in data}
    invalid = [row["name"] for row in data if not all((
        row["valid"], row["ready"], row["live"], row["immediate"], row["method"] == "btree",
        not row["partial"], not row["nulls_not_distinct"],
    ))]
    if len(actual) != len(data):
        invalid.append("duplicate index signatures")
    return inventory(actual, INDEXES, invalid)


def digest_definitions(data, keys):
    """Expose fingerprints for comparison without outputting SQL literals."""
    return [
        {key: (hashlib.sha256(value.encode()).hexdigest() if value is not None else None)
         if key in keys else value for key, value in row.items()}
        for row in data
    ]
