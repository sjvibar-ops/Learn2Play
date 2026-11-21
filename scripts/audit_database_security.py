"""Opt-in, catalog-only PostgreSQL security inventory. Do not run without approval.

No application imports, migrations, role switching, application-row reads, writes,
policy/function bodies, sequence values, or credentials are included in reports.
See docs/database-security-diagnostic.md before a separately authorized run.
"""
import argparse
import getpass
import json
import os
import re
import sys
import warnings


APPLICATION_TABLES = (
    "users", "appointments", "role_applications", "credentials", "role_application_history", "messages",
    "reports", "mentor_availability", "mentor_unavailable_dates", "payments",
    "xp_events", "attendance_events", "payment_events",
    "ledger_accounts", "ledger_transactions", "ledger_lines", "admin_audit_events",
)
REQUIRED_ACCESS = {
    "users": "SELECT INSERT UPDATE", "appointments": "SELECT INSERT UPDATE",
    "role_applications": "SELECT INSERT UPDATE", "credentials": "SELECT INSERT",
    "role_application_history": "SELECT INSERT",
    "messages": "SELECT INSERT UPDATE", "reports": "SELECT INSERT UPDATE",
    "mentor_availability": "SELECT INSERT DELETE",
    "mentor_unavailable_dates": "SELECT INSERT DELETE",
    "payments": "SELECT INSERT UPDATE", "xp_events": "SELECT INSERT",
    "attendance_events": "SELECT INSERT", "payment_events": "SELECT INSERT",
    "ledger_accounts": "SELECT INSERT", "ledger_transactions": "SELECT INSERT",
    "ledger_lines": "SELECT INSERT", "admin_audit_events": "SELECT INSERT",
}
SESSION_OPTIONS = (
    "-c default_transaction_read_only=on -c search_path=pg_catalog -c statement_timeout=15000 "
    "-c lock_timeout=2000 -c idle_in_transaction_session_timeout=60000"
)

# This scope also discovers similarly named application tables outside the
# requested schema. They are reported as candidates, never assumed to be ours.
SCOPE = """
WITH targets AS (
 SELECT c.oid, c.relname, c.relkind, c.relowner, c.relacl,
        c.relrowsecurity, c.relforcerowsecurity, n.nspname
 FROM pg_catalog.pg_class c
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('r','p') AND (
   (c.relname=ANY(%(tables)s::text[]) AND n.nspname NOT IN
     ('pg_catalog','information_schema','auth','storage') AND n.nspname !~ '^pg_')
   OR (n.nspname='storage' AND c.relname IN ('objects','buckets'))
 )
), subjects AS (
 SELECT oid, rolname, rolsuper, rolbypassrls FROM pg_catalog.pg_roles
 WHERE rolname IN ('anon','authenticated','service_role',current_user,session_user)
)
"""

IDENTITY = """
SELECT pg_catalog.current_database() AS database_name,
       current_user AS current_role, session_user AS session_role,
       pg_catalog.current_setting('session_authorization') AS session_authorization,
       pg_catalog.current_schema() AS current_schema,
       pg_catalog.current_setting('search_path')='pg_catalog' AS catalog_lookup_path_verified,
       pg_catalog.current_schemas(false) AS effective_schemas,
       pg_catalog.current_setting('server_version_num')::integer AS server_version,
       pg_catalog.current_setting('transaction_read_only') AS transaction_read_only,
       pg_catalog.current_setting('default_transaction_read_only') AS default_read_only,
       pg_catalog.current_setting('transaction_isolation') AS diagnostic_isolation,
       pg_catalog.current_setting('default_transaction_isolation') AS configured_default_isolation,
       pg_catalog.current_setting('row_security') AS row_security_setting,
       pg_catalog.pg_is_in_recovery() AS is_replica,
       pg_catalog.current_setting('session_replication_role') AS replication_role,
       pg_catalog.has_table_privilege(current_user,'pg_catalog.pg_stat_ssl','SELECT') AS can_inspect_ssl_transport,
       pg_catalog.has_table_privilege(current_user,'pg_catalog.pg_stat_gssapi','SELECT') AS can_inspect_gss_transport
"""

QUERIES = {
    "ssl_transport": """
SELECT ssl AS server_leg_ssl,version AS tls_version,cipher,bits AS cipher_bits
FROM pg_catalog.pg_stat_ssl WHERE pid=pg_catalog.pg_backend_pid()
""",
    "gss_transport": """
SELECT gss_authenticated,encrypted AS server_leg_gss_encrypted
FROM pg_catalog.pg_stat_gssapi WHERE pid=pg_catalog.pg_backend_pid()
""",
    "roles": """
SELECT r.oid, r.rolname, r.rolsuper, r.rolbypassrls, r.rolcreatedb,
       r.rolcreaterole, r.rolcanlogin, r.rolinherit, r.rolreplication,
       pg_catalog.pg_has_role(current_user,r.oid,'MEMBER') AS current_role_member,
       pg_catalog.pg_has_role(current_user,r.oid,'USAGE') AS current_role_inherits,
       CASE WHEN pg_catalog.current_setting('server_version_num')::integer>=160000
         THEN pg_catalog.pg_has_role(current_user,r.oid,'SET')
         ELSE pg_catalog.pg_has_role(current_user,r.oid,'MEMBER') END AS current_role_can_set
FROM pg_catalog.pg_roles r ORDER BY r.rolname
""",
    "role_database_settings": """
SELECT d.datname AS database_name,r.rolname AS role_name,
       pg_catalog.split_part(item,'=',1) AS setting_name,
       CASE WHEN pg_catalog.split_part(item,'=',1)='default_transaction_isolation'
                  AND pg_catalog.substr(item,pg_catalog.strpos(item,'=')+1) IN
                   ('read committed','read uncommitted','repeatable read','serializable')
              THEN pg_catalog.substr(item,pg_catalog.strpos(item,'=')+1)
            WHEN pg_catalog.split_part(item,'=',1) IN ('default_transaction_read_only','row_security')
                  AND pg_catalog.substr(item,pg_catalog.strpos(item,'=')+1) IN ('on','off')
              THEN pg_catalog.substr(item,pg_catalog.strpos(item,'=')+1)
            ELSE '<value omitted>' END AS safe_setting_value
FROM pg_catalog.pg_db_role_setting s
LEFT JOIN pg_catalog.pg_database d ON d.oid=s.setdatabase
LEFT JOIN pg_catalog.pg_roles r ON r.oid=s.setrole
CROSS JOIN LATERAL pg_catalog.unnest(s.setconfig) AS settings(item)
WHERE s.setdatabase IN (0,(SELECT oid FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database()))
 AND pg_catalog.split_part(item,'=',1) IN
  ('search_path','row_security','default_transaction_isolation','default_transaction_read_only')
ORDER BY d.datname,r.rolname,setting_name
""",
    "memberships": """
SELECT parent.rolname AS granted_role, member.rolname AS member_role,
       grantor.rolname AS grantor, m.admin_option,
       (pg_catalog.to_jsonb(m)->>'inherit_option')::boolean AS inherit_option,
       (pg_catalog.to_jsonb(m)->>'set_option')::boolean AS set_option
FROM pg_catalog.pg_auth_members m
JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid
JOIN pg_catalog.pg_roles member ON member.oid=m.member
LEFT JOIN pg_catalog.pg_roles grantor ON grantor.oid=m.grantor
ORDER BY parent.rolname,member.rolname
""",
    "tables": SCOPE + """
SELECT t.oid, t.nspname AS schema_name, t.relname AS table_name,
       pg_catalog.pg_get_userbyid(t.relowner) AS owner,
       t.relrowsecurity AS rls_enabled, t.relforcerowsecurity AS force_rls,
       pg_catalog.row_security_active(t.oid) AS rls_active_for_connection,
       pg_catalog.pg_has_role(current_user,t.relowner,'USAGE') AS connection_has_owner_privileges,
       (SELECT pg_catalog.count(*) FROM pg_catalog.pg_policy p WHERE p.polrelid=t.oid) AS policy_count
FROM targets t ORDER BY t.nspname,t.relname
""",
    "policies": SCOPE + """
SELECT t.nspname AS schema_name,t.relname AS table_name,p.polname AS policy_name,
       p.polcmd AS command,p.polpermissive AS permissive,
       ARRAY(SELECT CASE WHEN role_oid=0 THEN 'PUBLIC'
             ELSE pg_catalog.pg_get_userbyid(role_oid) END
             FROM pg_catalog.unnest(p.polroles) AS assigned(role_oid)) AS assigned_roles,
       p.polqual IS NOT NULL AS has_using_expression,
       p.polwithcheck IS NOT NULL AS has_check_expression,
       CASE WHEN p.polqual IS NULL THEN NULL ELSE
         pg_catalog.pg_get_expr(p.polqual,p.polrelid) IN ('true','(true)') END AS using_literal_true,
       CASE WHEN p.polwithcheck IS NULL THEN NULL ELSE
         pg_catalog.pg_get_expr(p.polwithcheck,p.polrelid) IN ('true','(true)') END AS check_literal_true
FROM targets t JOIN pg_catalog.pg_policy p ON p.polrelid=t.oid
ORDER BY t.nspname,t.relname,p.polname
""",
    "table_acl": SCOPE + """
SELECT t.nspname AS schema_name,t.relname AS table_name,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       pg_catalog.pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM targets t LEFT JOIN LATERAL
 pg_catalog.aclexplode(COALESCE(t.relacl,pg_catalog.acldefault('r',t.relowner))) a ON true
ORDER BY t.nspname,t.relname,grantee,a.privilege_type
""",
    "effective_table_privileges": SCOPE + """
SELECT t.nspname AS schema_name,t.relname AS table_name,r.rolname AS role_name,
       pg_catalog.has_schema_privilege(r.oid,t.nspname,'USAGE') AS schema_usage,
       op.privilege,CASE WHEN op.privilege='MAINTAIN' AND
         pg_catalog.current_setting('server_version_num')::integer<170000 THEN NULL
         ELSE pg_catalog.has_table_privilege(r.oid,t.oid,op.privilege) END AS table_granted,
       CASE WHEN op.privilege IN ('SELECT','INSERT','UPDATE','REFERENCES') THEN
         pg_catalog.has_any_column_privilege(r.oid,t.oid,op.privilege) ELSE NULL END AS any_column_granted,
       op.privilege IN ('SELECT','INSERT','UPDATE','DELETE') AND
       t.relrowsecurity AND NOT (r.rolsuper OR r.rolbypassrls OR
         (NOT t.relforcerowsecurity AND pg_catalog.pg_has_role(r.oid,t.relowner,'USAGE')))
         AS rls_restricts_operation,
       op.privilege IN ('SELECT','INSERT','UPDATE','DELETE') AND
       EXISTS (SELECT 1 FROM pg_catalog.pg_policy p
         WHERE p.polrelid=t.oid AND p.polpermissive AND p.polcmd::text IN ('*',op.command)
         AND EXISTS (SELECT 1 FROM pg_catalog.unnest(p.polroles) AS assigned(role_oid)
           WHERE CASE WHEN role_oid=0 THEN true ELSE
             pg_catalog.pg_has_role(r.oid,role_oid,'USAGE') END)) AS applicable_permissive_policy
FROM targets t CROSS JOIN subjects r CROSS JOIN
 (VALUES ('SELECT','r'),('INSERT','a'),('UPDATE','w'),('DELETE','d'),
         ('TRUNCATE','!'),('REFERENCES','!'),('TRIGGER','!'),
         ('MAINTAIN','!')) op(privilege,command)
ORDER BY t.nspname,t.relname,r.rolname,op.privilege
""",
    "effective_column_privileges": SCOPE + """
SELECT t.nspname AS schema_name,t.relname AS table_name,a.attname AS column_name,
       pg_catalog.format_type(a.atttypid,a.atttypmod) AS data_type,
       a.attnotnull AS not_null,a.atthasdef AS has_default,
       a.attidentity AS identity_generation,a.attgenerated AS generated_kind,r.rolname AS role_name,
       pg_catalog.has_column_privilege(r.oid,t.oid,a.attnum,'SELECT') AS can_select,
       pg_catalog.has_column_privilege(r.oid,t.oid,a.attnum,'INSERT') AS can_insert,
       pg_catalog.has_column_privilege(r.oid,t.oid,a.attnum,'UPDATE') AS can_update,
       pg_catalog.has_column_privilege(r.oid,t.oid,a.attnum,'REFERENCES') AS can_reference
FROM targets t JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum>0 AND NOT a.attisdropped
CROSS JOIN subjects r ORDER BY t.nspname,t.relname,a.attnum,r.rolname
""",
    "column_acl": SCOPE + """
SELECT t.nspname AS schema_name,t.relname AS table_name,c.attname AS column_name,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       pg_catalog.pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM targets t JOIN pg_catalog.pg_attribute c ON c.attrelid=t.oid AND c.attnum>0 AND NOT c.attisdropped
CROSS JOIN LATERAL pg_catalog.aclexplode(c.attacl) a
ORDER BY t.nspname,t.relname,c.attnum,grantee
""",
    "schema_acl": """
SELECT n.nspname AS schema_name,pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       pg_catalog.pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM pg_catalog.pg_namespace n LEFT JOIN LATERAL
 pg_catalog.aclexplode(COALESCE(n.nspacl,pg_catalog.acldefault('n',n.nspowner))) a ON true
WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
ORDER BY n.nspname,grantee
""",
    "effective_schema_privileges": """
SELECT n.nspname AS schema_name,r.rolname AS role_name,
       pg_catalog.has_schema_privilege(r.oid,n.oid,'USAGE') AS can_use,
       pg_catalog.has_schema_privilege(r.oid,n.oid,'CREATE') AS can_create
FROM pg_catalog.pg_namespace n CROSS JOIN pg_catalog.pg_roles r
WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND r.rolname IN ('anon','authenticated','service_role',current_user,session_user)
ORDER BY n.nspname,r.rolname
""",
    "sequences": SCOPE + """
SELECT n.nspname AS schema_name,s.relname AS sequence_name,
       pg_catalog.pg_get_userbyid(s.relowner) AS owner,
       r.rolname AS role_name,
       pg_catalog.has_schema_privilege(r.oid,n.oid,'USAGE') AS schema_usage,
       pg_catalog.has_sequence_privilege(r.oid,s.oid,'USAGE') AS can_use,
       pg_catalog.has_sequence_privilege(r.oid,s.oid,'SELECT') AS can_select,
       pg_catalog.has_sequence_privilege(r.oid,s.oid,'UPDATE') AS can_update,
       ARRAY(SELECT DISTINCT t.nspname||'.'||t.relname
         FROM targets t WHERE
          EXISTS (SELECT 1 FROM pg_catalog.pg_depend d WHERE d.classid='pg_catalog.pg_class'::regclass
            AND d.objid=s.oid AND d.refobjid=t.oid AND d.deptype IN ('a','i'))
          OR EXISTS (SELECT 1 FROM pg_catalog.pg_attrdef ad JOIN pg_catalog.pg_depend d
            ON d.classid='pg_catalog.pg_attrdef'::regclass AND d.objid=ad.oid
            WHERE ad.adrelid=t.oid AND d.refobjid=s.oid AND d.refclassid='pg_catalog.pg_class'::regclass)
       ) AS associated_tables,
       EXISTS (SELECT 1 FROM pg_catalog.pg_depend d JOIN targets t ON t.oid=d.refobjid
          WHERE d.classid='pg_catalog.pg_class'::regclass AND d.refclassid='pg_catalog.pg_class'::regclass
            AND d.objid=s.oid AND d.deptype='i') AS identity_owned,
       EXISTS (SELECT 1 FROM pg_catalog.pg_attrdef ad JOIN targets t ON t.oid=ad.adrelid
          JOIN pg_catalog.pg_depend d ON d.classid='pg_catalog.pg_attrdef'::regclass AND d.objid=ad.oid
          WHERE d.refobjid=s.oid AND d.refclassid='pg_catalog.pg_class'::regclass) AS referenced_by_column_default
FROM pg_catalog.pg_class s JOIN pg_catalog.pg_namespace n ON n.oid=s.relnamespace CROSS JOIN subjects r
WHERE s.relkind='S' AND (
 n.nspname=%(schema)s OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend d JOIN targets t ON t.oid=d.refobjid
   WHERE d.classid='pg_catalog.pg_class'::regclass AND d.objid=s.oid AND d.deptype IN ('a','i'))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_attrdef ad JOIN targets t ON t.oid=ad.adrelid
   JOIN pg_catalog.pg_depend d ON d.classid='pg_catalog.pg_attrdef'::regclass AND d.objid=ad.oid
   WHERE d.refobjid=s.oid AND d.refclassid='pg_catalog.pg_class'::regclass))
ORDER BY n.nspname,s.relname,r.rolname
""",
    "sequence_acl": SCOPE + """
SELECT n.nspname AS schema_name,s.relname AS sequence_name,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       pg_catalog.pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM pg_catalog.pg_class s JOIN pg_catalog.pg_namespace n ON n.oid=s.relnamespace LEFT JOIN LATERAL
 pg_catalog.aclexplode(COALESCE(s.relacl,pg_catalog.acldefault('s',s.relowner))) a ON true
WHERE s.relkind='S' AND (n.nspname=%(schema)s
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend d JOIN targets t ON t.oid=d.refobjid
   WHERE d.classid='pg_catalog.pg_class'::regclass AND d.objid=s.oid AND d.deptype IN ('a','i'))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_attrdef ad JOIN targets t ON t.oid=ad.adrelid
   JOIN pg_catalog.pg_depend d ON d.classid='pg_catalog.pg_attrdef'::regclass AND d.objid=ad.oid
   WHERE d.refobjid=s.oid AND d.refclassid='pg_catalog.pg_class'::regclass))
ORDER BY n.nspname,s.relname,grantee
""",
    "default_acl": """
SELECT creator.rolname AS creating_role,COALESCE(n.nspname,'<global>') AS schema_name,
       d.defaclobjtype AS object_type,pg_catalog.cardinality(d.defaclacl)=0 AS explicitly_empty_acl,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       pg_catalog.pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM pg_catalog.pg_default_acl d JOIN pg_catalog.pg_roles creator ON creator.oid=d.defaclrole
LEFT JOIN pg_catalog.pg_namespace n ON n.oid=d.defaclnamespace
LEFT JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) a ON true
ORDER BY creator.rolname,schema_name,d.defaclobjtype,grantee
""",
    "views": """
SELECT n.nspname AS schema_name,c.relname AS view_name,c.relkind,
       pg_catalog.pg_get_userbyid(c.relowner) AS owner,
       COALESCE('security_invoker=true'=ANY(c.reloptions),false) AS security_invoker,
       COALESCE('security_barrier=true'=ANY(c.reloptions),false) AS security_barrier,
       r.rolname AS role_name,pg_catalog.has_schema_privilege(r.oid,n.oid,'USAGE') AS schema_usage,
       pg_catalog.has_table_privilege(r.oid,c.oid,'SELECT') AS can_select,
       pg_catalog.has_table_privilege(r.oid,c.oid,'INSERT') AS can_insert,
       pg_catalog.has_table_privilege(r.oid,c.oid,'UPDATE') AS can_update,
       pg_catalog.has_table_privilege(r.oid,c.oid,'DELETE') AS can_delete,
       pg_catalog.has_any_column_privilege(r.oid,c.oid,'SELECT') AS any_column_select,
       pg_catalog.has_any_column_privilege(r.oid,c.oid,'INSERT') AS any_column_insert,
       pg_catalog.has_any_column_privilege(r.oid,c.oid,'UPDATE') AS any_column_update,
       ARRAY(SELECT DISTINCT bn.nspname||'.'||b.relname FROM pg_catalog.pg_rewrite rw
         JOIN pg_catalog.pg_depend d ON d.classid='pg_catalog.pg_rewrite'::regclass AND d.objid=rw.oid
         JOIN pg_catalog.pg_class b ON b.oid=d.refobjid AND d.refclassid='pg_catalog.pg_class'::regclass
         JOIN pg_catalog.pg_namespace bn ON bn.oid=b.relnamespace
         WHERE rw.ev_class=c.oid AND b.oid<>c.oid) AS direct_relation_dependencies
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
CROSS JOIN pg_catalog.pg_roles r WHERE c.relkind IN ('v','m')
 AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND r.rolname IN ('anon','authenticated','service_role',current_user,session_user)
ORDER BY n.nspname,c.relname,r.rolname
""",
    "view_column_privileges": """
SELECT n.nspname AS schema_name,c.relname AS view_name,a.attname AS column_name,
       r.rolname AS role_name,
       pg_catalog.has_schema_privilege(r.oid,n.oid,'USAGE') AS schema_usage,
       pg_catalog.has_column_privilege(r.oid,c.oid,a.attnum,'SELECT') AS can_select,
       pg_catalog.has_column_privilege(r.oid,c.oid,a.attnum,'INSERT') AS can_insert,
       pg_catalog.has_column_privilege(r.oid,c.oid,a.attnum,'UPDATE') AS can_update
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
CROSS JOIN pg_catalog.pg_roles r
WHERE c.relkind IN ('v','m') AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND r.rolname IN ('anon','authenticated','service_role',current_user,session_user)
ORDER BY n.nspname,c.relname,a.attnum,r.rolname
""",
    "view_acl": """
SELECT n.nspname AS schema_name,c.relname AS view_name,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       pg_catalog.pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN LATERAL pg_catalog.aclexplode(COALESCE(c.relacl,pg_catalog.acldefault('r',c.relowner))) a ON true
WHERE c.relkind IN ('v','m') AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
ORDER BY n.nspname,c.relname,grantee,a.privilege_type
""",
    "view_column_acl": """
SELECT n.nspname AS schema_name,c.relname AS view_name,b.attname AS column_name,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       pg_catalog.pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
JOIN pg_catalog.pg_attribute b ON b.attrelid=c.oid AND b.attnum>0 AND NOT b.attisdropped
LEFT JOIN LATERAL pg_catalog.aclexplode(b.attacl) a ON true
WHERE c.relkind IN ('v','m') AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND b.attacl IS NOT NULL
ORDER BY n.nspname,c.relname,b.attnum,grantee,a.privilege_type
""",
    "functions": """
SELECT n.nspname AS schema_name,p.proname AS function_name,p.oid,p.prokind,
       pg_catalog.pg_get_userbyid(p.proowner) AS owner,p.prosecdef AS security_definer,
       pg_catalog.pg_get_function_identity_arguments(p.oid) AS identity_arguments,
       pg_catalog.pg_get_function_result(p.oid) AS result_type,
       EXISTS (SELECT 1 FROM pg_catalog.unnest(p.proconfig) AS settings(setting) WHERE setting ~ '^search_path=') AS has_search_path_setting,
       r.rolname AS role_name,pg_catalog.has_schema_privilege(r.oid,n.oid,'USAGE') AS schema_usage,
       pg_catalog.has_function_privilege(r.oid,p.oid,'EXECUTE') AS can_execute
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
CROSS JOIN pg_catalog.pg_roles r WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND r.rolname IN ('anon','authenticated','service_role',current_user,session_user)
ORDER BY n.nspname,p.proname,p.oid,r.rolname
""",
    "function_acl": """
SELECT n.nspname AS schema_name,p.proname AS function_name,p.oid,
       CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       pg_catalog.pg_get_userbyid(a.grantor) AS grantor,a.privilege_type,a.is_grantable
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace LEFT JOIN LATERAL
 pg_catalog.aclexplode(COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))) a ON true
WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
ORDER BY n.nspname,p.proname,p.oid,grantee
""",
    "triggers": SCOPE + """
SELECT t.nspname AS schema_name,t.relname AS table_name,g.tgname AS trigger_name,
       g.tgenabled AS enabled_mode,g.tgisinternal AS internal,g.tgtype AS type_bits,g.tgqual IS NOT NULL AS has_when_condition,
       fn.oid AS function_oid,pn.nspname AS function_schema,fn.proname AS function_name,
       pg_catalog.pg_get_userbyid(fn.proowner) AS function_owner,
       fn.prosecdef AS security_definer
FROM targets t JOIN pg_catalog.pg_trigger g ON g.tgrelid=t.oid
JOIN pg_catalog.pg_proc fn ON fn.oid=g.tgfoid
JOIN pg_catalog.pg_namespace pn ON pn.oid=fn.pronamespace ORDER BY t.nspname,t.relname,g.tgname
""",
    "event_triggers": """
SELECT e.evtname,e.evtevent,e.evtenabled,pg_catalog.pg_get_userbyid(e.evtowner) AS owner,
       e.evttags,p.oid AS function_oid,n.nspname AS function_schema,p.proname AS function_name,
       pg_catalog.pg_get_userbyid(p.proowner) AS function_owner,p.prosecdef AS security_definer
FROM pg_catalog.pg_event_trigger e JOIN pg_catalog.pg_proc p ON p.oid=e.evtfoid
JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace ORDER BY e.evtname
""",
}


class DiagnosticError(Exception):
    """Messages must be fixed strings, never driver messages or user credentials."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo an accidentally supplied secret.
        raise DiagnosticError("Invalid arguments. Consult the documented non-secret options.")


SAFE_CATALOG_FUNCTIONS = {
    "current_database", "current_schema", "current_schemas", "current_setting",
    "pg_is_in_recovery", "pg_backend_pid", "pg_has_role", "to_jsonb", "pg_get_userbyid",
    "row_security_active", "count", "unnest", "pg_get_expr", "aclexplode", "acldefault",
    "has_schema_privilege", "has_table_privilege", "has_any_column_privilege",
    "format_type", "has_column_privilege", "has_sequence_privilege",
    "pg_get_function_identity_arguments", "pg_get_function_result", "has_function_privilege",
    "split_part", "substr", "strpos", "cardinality",
}
SAFE_SQL_CONSTRUCTS = {"AS", "IN", "EXISTS", "ANY", "COALESCE", "VALUES", "NOT", "AND", "OR", "ARRAY", "CAST", "JOIN", "FROM", "WHERE", "ON", "SELECT", "WITH"}


def inspect_query(connection, sql, parameters, limit):
    # Accidental-change safeguard for these fixed queries, NOT a general SQL parser/sandbox.
    if ';' in sql or not re.match(r'\s*(SELECT|WITH)\b', sql, re.I):
        raise DiagnosticError("Unsafe diagnostic statement rejected before execution.")
    if re.search(r'\b(INSERT|UPDATE|DELETE|TRUNCATE|CREATE|ALTER|DROP|GRANT|REVOKE|CALL|COPY|DO|SET|RESET)\b',
                 re.sub(r"'(?:''|[^'])*'", "''", sql), re.I):
        raise DiagnosticError("Non-read-only diagnostic statement rejected before execution.")
    if re.search(r'\b(nextval|setval|pg_advisory_lock|pg_terminate_backend|pg_cancel_backend)\s*\(', sql, re.I):
        raise DiagnosticError("State-changing function rejected before execution.")
    scrubbed = re.sub(r"'(?:''|[^'])*'", "''", sql)
    # SQL table-function/VALUES aliases have column lists, not function calls.
    scrubbed = re.sub(r'(?<=\))\s+(?:AS\s+)?(?:assigned|settings|op)\s*\([A-Za-z_,\s]+\)',
                      ' alias_columns',scrubbed,flags=re.I)
    for namespace, name in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(',scrubbed):
        if namespace!='pg_catalog' or name not in SAFE_CATALOG_FUNCTIONS:
            raise DiagnosticError("Non-allowlisted catalog function rejected before execution.")
    for match in re.finditer(r'(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_]*)\s*\(',scrubbed):
        if match.group(1).upper() not in SAFE_SQL_CONSTRUCTS:
            raise DiagnosticError("Unqualified function rejected before execution.")
    # Narrow relation-name check; quoted SQL and other SQL syntax still require source review.
    for namespace, relation in re.findall(r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?',scrubbed,re.I):
        if (relation and namespace!='pg_catalog') or (not relation and namespace not in ('targets','subjects','LATERAL')):
            raise DiagnosticError("Non-catalog relation rejected before execution.")
    bounded_sql = sql + "\n LIMIT %(row_limit)s"
    available = dict(parameters or {},row_limit=limit+1)
    wanted = set(re.findall(r'%\(([a-z_]+)\)s',bounded_sql))
    if not wanted <= set(available):
        raise DiagnosticError("Unknown diagnostic query parameter rejected.")
    with connection.cursor() as cursor:
        cursor.execute(bounded_sql,{key:available[key] for key in wanted})
        rows = cursor.fetchmany(limit + 1)
    return {"status": "ok", "truncated": len(rows) > limit, "rows": rows[:limit]}


def assess(sections, identity, schema):
    findings = []
    def add(severity, code, message, **metadata):
        findings.append(dict(severity=severity, code=code, message=message, **metadata))
    for name, section in sections.items():
        if section['status'] != 'ok' or section.get('truncated'):
            add('MEDIUM', 'INCOMPLETE_SECTION', 'Unavailable or truncated metadata is not evidence of safety.', section=name)
    tables = sections['tables'].get('rows', [])
    by_name = {r['table_name']: r for r in tables if r['schema_name'] == schema}
    complete_tables = sections['tables']['status']=='ok' and not sections['tables'].get('truncated')
    for name in (*APPLICATION_TABLES, 'alembic_version'):
        if name not in by_name:
            add('HIGH' if complete_tables else 'INFORMATIONAL',
                'MISSING_TABLE' if complete_tables else 'TABLE_METADATA_UNKNOWN',
                'Expected relation was not found in complete metadata.' if complete_tables else 'Incomplete metadata cannot establish whether this table exists.',table=name)

    roles = sections['roles'].get('rows', [])
    runtime = next((r for r in roles if r['rolname'] == identity['current_role']), None)
    if runtime:
        excessive = [key for key in ('rolsuper','rolbypassrls','rolcreatedb','rolcreaterole','rolreplication') if runtime[key]]
        if excessive:
            add('HIGH','PRIVILEGED_CONNECTION','Connection has attributes not normally needed by Flask.', attributes=excessive)
    if identity['configured_default_isolation']!='read committed':
        add('HIGH','REVIEW_BOOKING_ISOLATION','Role/database default may violate booking mutex READ COMMITTED requirements; diagnostic snapshot isolation is intentionally different.')
    if identity.get('replication_role') == 'replica':
        add('HIGH','REPLICA_TRIGGER_MODE','Session is in replica trigger mode; origin-only audit guards may not run.')
    if identity['is_replica']:
        add('HIGH','READ_ONLY_REPLICA','This connection is to a recovering replica, not a write-capable Flask target.')
    for role in roles:
        if role['rolname']!=identity['current_role'] and role['current_role_can_set'] and any(
                role[key] for key in ('rolsuper','rolbypassrls','rolcreatedb','rolcreaterole','rolreplication')):
            add('HIGH','PRIVILEGED_MEMBERSHIP','Connection can potentially assume a privileged role; membership does not imply all attributes are inherited.',role=role['rolname'])
    for name in ('objects','buckets'):
        if not any(t['schema_name']=='storage' and t['table_name']==name for t in tables):
            add('INFORMATIONAL','STORAGE_METADATA_NOT_FOUND','Storage configuration is not established by absent catalog metadata.',table=name)
    for table in tables:
        if table['schema_name'] == schema and table['table_name'] in APPLICATION_TABLES:
            if table['connection_has_owner_privileges']:
                add('MEDIUM','OWNER_PRIVILEGES','Runtime ownership can permit bypass/DDL; audit triggers are not owner-proof.', table=table['table_name'])
            owner_role = next((r for r in roles if r['rolname']==table['owner']),None)
            if owner_role and owner_role['rolname']!=identity['current_role'] and owner_role['current_role_can_set'] and not table['connection_has_owner_privileges']:
                add('HIGH','CAN_ASSUME_TABLE_OWNER','Connection can potentially assume table ownership privileges through SET ROLE; no role switch was attempted.',table=table['table_name'],owner=table['owner'])
            if not table['rls_enabled']:
                add('HIGH','RLS_NOT_ENABLED','Review client ACLs and exposure; this is not proof of a reachable Data API leak.', table=table['table_name'])
    effective = sections['effective_table_privileges'].get('rows', [])
    compatibility = []
    for name, operations in REQUIRED_ACCESS.items():
        checks = []
        for op in operations.split():
            row = next((r for r in effective if r['schema_name']==schema and r['table_name']==name
                        and r['role_name']==identity['current_role'] and r['privilege']==op), None)
            if row is None:
                outcome = 'UNKNOWN'
            elif not row['schema_usage']:
                outcome = 'BLOCKED_SCHEMA_USAGE'
            elif op!='SELECT' and identity['is_replica']:
                outcome = 'BLOCKED_REPLICA'
            elif not row['table_granted'] and not row['any_column_granted']:
                outcome = 'BLOCKED_PRIVILEGE'
            elif row['rls_restricts_operation'] and identity['row_security_setting']=='off':
                outcome = 'BLOCKED_ROW_SECURITY_OFF_NOT_A_BYPASS'
            elif row['rls_restricts_operation'] and not row['applicable_permissive_policy']:
                outcome = 'BLOCKED_RLS_NO_APPLICABLE_PERMISSIVE_POLICY'
            elif not row['table_granted']:
                outcome = 'REVIEW_COLUMN_GRANTS'
            elif row['rls_restricts_operation']:
                outcome = 'REVIEW_POLICY_EXPRESSIONS'
            else:
                outcome = 'ACL_AND_RLS_APPEAR_POSSIBLE_NOT_OPERATIONALLY_PROVEN'
            if outcome.startswith('BLOCKED_'):
                add('HIGH','RUNTIME_OPERATION_BLOCKED','Metadata indicates an ACL/RLS/replica block for the connected SQL role; confirm the real Flask identity. No application operation was executed.',
                    table=name,operation=op,assessment=outcome)
            checks.append(dict(operation=op, assessment=outcome))
        compatibility.append(dict(table=name, checks=checks))
    for row in effective:
        if row['role_name'] in ('anon','authenticated') and row['schema_usage'] and (
                row['table_granted'] or row['any_column_granted']):
            add('HIGH' if not row['rls_restricts_operation'] else 'MEDIUM', 'CLIENT_PRIVILEGE_PATH',
                'Client has ACL access; reachability depends on API/schema settings and policy semantics.',
                schema=row['schema_name'], table=row['table_name'], role=row['role_name'], operation=row['privilege'])
        if row['role_name']==identity['current_role'] and row['schema_name']==schema:
            if row['privilege'] in ('TRUNCATE','TRIGGER','REFERENCES','MAINTAIN') and row['table_granted']:
                add('MEDIUM','REVIEW_EXCESS_RUNTIME_PRIVILEGE','This privilege is not needed by current Flask operations.',table=row['table_name'],operation=row['privilege'])
            if row['table_name'] in ('payment_events','attendance_events','xp_events','role_application_history') and row['privilege'] in ('UPDATE','DELETE') and (row['table_granted'] or row['any_column_granted']):
                add('MEDIUM','APPEND_TABLE_MUTATION_GRANT','Append-oriented runtime tables do not need this ACL; separate guards may block mutation.',table=row['table_name'],operation=row['privilege'])
    for row in effective:
        if row['role_name']==identity['current_role'] and row['schema_name']==schema and row['table_name']=='alembic_version' and row['privilege'] in ('INSERT','UPDATE','DELETE','TRUNCATE') and (row['table_granted'] or row['any_column_granted']):
            add('MEDIUM','MIGRATION_STATE_WRITE','Normal Flask requests do not need to change migration state.',operation=row['privilege'])
    for row in sections['views'].get('rows', []):
        if row['role_name'] in ('anon','authenticated') and row['schema_usage']:
            operations = [op.upper() for op in ('select','insert','update','delete')
                          if row['can_'+op] or (op!='delete' and row['any_column_'+op])]
            if operations:
                add('MEDIUM','REVIEW_CLIENT_VIEW','View ACL/column paths need exposure, updatability and dependency review; definitions/data were not read.',
                    schema=row['schema_name'],view=row['view_name'],role=row['role_name'],operations=operations)
    for row in sections['effective_schema_privileges'].get('rows', []):
        if row['role_name']==identity['current_role'] and row['can_create']:
            add('MEDIUM','SCHEMA_CREATE','Runtime schema creation is not needed by normal Flask requests.',schema=row['schema_name'])
    for row in sections['sequences'].get('rows', []):
        if row['role_name']==identity['current_role'] and set(row['associated_tables']) & {f'{schema}.{name}' for name in APPLICATION_TABLES}:
            if not row['schema_usage'] or not (row['can_use'] or row['can_update']):
                add('HIGH' if row['referenced_by_column_default'] and not row['identity_owned'] else 'INFORMATIONAL',
                    'REVIEW_SEQUENCE_ACCESS','Sequence ACL is absent; serial/default expressions may need it while identity generation can differ. No generator was called.',schema=row['schema_name'],sequence=row['sequence_name'])
            if row['can_update']:
                add('MEDIUM','SEQUENCE_UPDATE','Runtime sequence mutation privileges are unnecessary for normal inserts.',sequence=row['sequence_name'])
    for row in sections['functions'].get('rows', []):
        if row['role_name'] in ('anon','authenticated') and row['schema_usage'] and row['can_execute'] and row['security_definer']:
            add('HIGH','REVIEW_DEFINER_FUNCTION','Client-executable definer function needs separate body/exposure review; not proof of exploit.',schema=row['schema_name'],function_oid=row['oid'])
    expected_guards = {
        'payment_events': ('prevent_payment_event_mutation','payment_events_no_mutation','payment_events_no_truncate'),
        'role_application_history': ('prevent_role_application_history_mutation','role_application_history_no_mutation','role_application_history_no_truncate'),
    }
    complete_triggers = sections['triggers']['status']=='ok' and not sections['triggers'].get('truncated')
    for table, (function, mutation, truncate) in expected_guards.items():
        guards = {r['trigger_name'] for r in sections['triggers'].get('rows', [])
                  if r['schema_name']==schema and r['table_name']==table and r['enabled_mode'] in ('O','A')
                  and r['function_schema']==schema and r['function_name']==function
                  and not r['security_definer'] and not r['internal'] and not r['has_when_condition']
                  and r['type_bits']==(27 if r['trigger_name']==mutation else 34)}
        if table in by_name and not {mutation,truncate} <= guards:
            add('HIGH' if complete_triggers else 'INFORMATIONAL',
                'AUDIT_GUARD_METADATA_MISSING' if complete_triggers else 'AUDIT_GUARD_METADATA_UNKNOWN',
                'Expected audit guard metadata is missing, disabled or different; bodies and real behavior remain unverified.' if complete_triggers else 'Incomplete metadata cannot establish audit guard presence.',table=table)

    return findings, compatibility


class ConnectionScope:
    """Rollback and close without the driver's raw-error-logging __exit__."""
    def __init__(self, connection, progress):
        self.connection = connection
        self.progress = progress

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        previous = self.progress['stage']
        self.progress['stage'] = 'connection_cleanup'
        failed = False
        # Catch even interruptions only within cleanup so close is always
        # attempted and an existing diagnostic failure remains primary.
        try:
            self.connection.rollback()
        except BaseException:
            failed = True
        try:
            self.connection.close()
        except BaseException:
            failed = True
        if failed:
            self.progress['cleanup_failed'] = True
            if exc_type is None:
                raise RuntimeError('Diagnostic connection cleanup failed.') from None
        self.progress['stage'] = previous
        return False


def main(argv=None, progress=None):
    progress = progress if progress is not None else {}
    progress['stage'] = 'preflight'
    parser = SafeParser(description=__doc__)
    parser.add_argument('--allow-connect', action='store_true', help='Use only after separate approval; still requires interactive consent.')
    parser.add_argument('--expected-host', required=True)
    parser.add_argument('--expected-port', type=int, default=5432)
    parser.add_argument('--expected-database', required=True)
    parser.add_argument('--schema', default='public')
    parser.add_argument('--expected-runtime-role', help='Optional expected SQL role; not the pooler username. Mismatch stops inspection.')
    parser.add_argument('--use-database-url', action='store_true', help='Read existing DATABASE_URL without changing it; otherwise use secure prompting.')
    parser.add_argument('--max-rows', type=int, default=20000, help='Per-section output cap. Truncation is explicitly reported.')
    args = parser.parse_args(argv)
    if not args.allow_connect:
        raise DiagnosticError('Connection disabled. Separate approval and --allow-connect are required.')
    if not sys.stdin.isatty() or not 1 <= args.expected_port <= 65535 or not 1 <= args.max_rows <= 100000:
        raise DiagnosticError('Interactive authorization and valid bounds are required.')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+',args.expected_host):
        raise DiagnosticError('An explicit single TCP host is required.')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',args.schema) or args.schema.startswith('pg_') or args.schema in ('auth','storage','information_schema'):
        raise DiagnosticError('Use a non-system application schema.')
    if not args.expected_database or any(ord(c)<32 for c in args.expected_database):
        raise DiagnosticError('An explicit database name is required.')
    if any(os.environ.get(key) for key in ('PGSERVICE','PGSERVICEFILE','PGHOSTADDR','PGOPTIONS')):
        raise DiagnosticError('Ambient connection-routing/startup overrides are not supported; use an approved clean terminal context.')
    print(json.dumps(dict(approved_host=args.expected_host, approved_port=args.expected_port,
                         approved_database=args.expected_database, application_schema=args.schema)))
    print('No connection has been made. Continue only with separate authorization for this target.')
    progress['stage'] = 'authorization'
    if input('Type APPROVE READ ONLY to connect: ') != 'APPROVE READ ONLY':
        raise DiagnosticError('Authorization not supplied; no connection made.')
    progress['stage'] = 'driver_import'
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row
    from psycopg import IsolationLevel
    progress['stage'] = 'credential_input'
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        dsn = os.environ.get('DATABASE_URL') if args.use_database_url else getpass.getpass('PostgreSQL URL/conninfo (hidden): ')
    if not dsn:
        raise DiagnosticError('No connection credential supplied.')
    progress['stage'] = 'url_parsing'
    for prefix in ('postgresql+psycopg://','postgresql+psycopg2://','postgres://'):
        if dsn.startswith(prefix):
            dsn = 'postgresql://' + dsn[len(prefix):]
            break
    parameters = conninfo_to_dict(dsn)
    progress['stage'] = 'connection_configuration'
    if any(key in parameters for key in ('service','hostaddr','options','replication','target_session_attrs')):
        raise DiagnosticError('Ambiguous/service/startup/replication connection options are not accepted.')
    if (parameters.get('host','').lower()!=args.expected_host.lower()
            or int(parameters.get('port','5432'))!=args.expected_port
            or parameters.get('dbname')!=args.expected_database or not parameters.get('user')):
        raise DiagnosticError('Credential target does not match the approved explicit host/port/database.')
    progress['stage'] = 'tls_configuration'
    if parameters.get('sslmode','verify-full')!='verify-full':
        raise DiagnosticError('TLS hostname verification requires sslmode=verify-full.')
    if parameters.get('gssencmode','disable')!='disable':
        raise DiagnosticError('GSS encryption overrides are not accepted; TLS is required.')
    parameters.update(sslmode='verify-full',gssencmode='disable')
    parameters.update(host=args.expected_host,port=str(args.expected_port),dbname=args.expected_database,
                      options=SESSION_OPTIONS,connect_timeout=10)
    report = {"verdict": "METADATA_ONLY_NOT_A_SECURITY_CERTIFICATION", "scope": "Catalog metadata only; not a proof of absence of all exposures.",
              "limitations": [
                  "No application records, credential values, sequence values, function/view bodies or policy expressions are printed.",
                  "ACL checks do not prove row visibility or application success; policies and column grants require semantic review.",
                  "API exposure settings, other projects, dynamic SQL and outside services are not verified.",
                  "Compatibility applies to the connected SQL role only; verify that this is the actual Flask runtime role.",
                  "Default ACL absence is not absence of built-in/platform defaults; creator roles and global ACLs matter.",
                  "Membership metadata is version-dependent; pre-16 inherit/set fields are null and SET capability uses legacy membership semantics.",
                  "RLS does not restrict TRUNCATE/REFERENCES/TRIGGER/MAINTAIN; ACL paths for these are SQL/RPC capabilities, not assertions of REST endpoints.",
                  "Storage bucket privacy flags and application records are not read. Catalog policy metadata alone cannot establish receipt isolation.",
                  "Metadata snapshots cannot authenticate external financial authority or prove audit function behavior.",
                  "Diagnostic read-only/timeouts/isolation/search_path intentionally differ from Flask; original runtime read-only defaults and application schema resolution are unverified.",
                  "Treat output as sensitive metadata. No security findings are automatic configuration changes.",
                  "Server transport metadata describes the PostgreSQL-facing leg, which can differ from client TLS behind a pooler; missing metadata is unverified.",
                  "The regex statement guard is an accidental-change aid, not a general SQL parser or arbitrary-SQL sandbox.",
                  "Identity-generated columns and custom defaults can have different sequence permission requirements; associations are metadata, not DML probes.",
              ]}
    catalog_parameters = dict(schema=args.schema,tables=list(APPLICATION_TABLES)+['alembic_version'])
    # Psycopg emits transaction control, not DDL/DML. Both startup default and
    # the actual transaction must be read-only. No permissive fallback exists.
    progress['stage'] = 'connection_open'
    with ConnectionScope(psycopg.connect(**parameters,autocommit=False,prepare_threshold=None,row_factory=dict_row), progress) as connection:
        progress['stage'] = 'transaction_configuration'
        connection.read_only = True
        connection.isolation_level = IsolationLevel.REPEATABLE_READ
        progress['stage'] = 'identity_query'
        identity = inspect_query(connection,IDENTITY,None,1)['rows'][0]
        progress['stage'] = 'read_only_verification'
        if identity['transaction_read_only']!='on' or identity['default_read_only']!='on':
            # Report only known setting states, never arbitrary result values
            # or the full identity before its safety checks have passed.
            states = {}
            for key in ('transaction_read_only','default_read_only'):
                value = identity[key]
                states[key] = value if isinstance(value,str) and value in ('on','off') else 'unexpected'
            print(json.dumps({'read_only_verification': states}),file=sys.stderr,flush=True)
            raise DiagnosticError('Server-side read-only enforcement was not established; stopped.')
        progress['stage'] = 'startup_verification'
        if not identity['catalog_lookup_path_verified']:
            raise DiagnosticError('Catalog lookup search_path was not established; stopped.')
        progress['stage'] = 'database_verification'
        if identity['database_name']!=args.expected_database or identity['server_version']<120000:
            raise DiagnosticError('Unexpected database identity or unsupported PostgreSQL version; stopped.')
        progress['stage'] = 'runtime_role_verification'
        if args.expected_runtime_role and identity['current_role']!=args.expected_runtime_role:
            raise DiagnosticError('Connected SQL role differs from the expected runtime role; stopped.')
        progress['stage'] = 'identity_output'
        print(json.dumps({"verified_identity": identity},ensure_ascii=True),flush=True)
        report['identity'] = identity
        sections = {}
        for name, sql in QUERIES.items():
            progress['stage'] = 'catalog_inventory'
            transport_capability = {'ssl_transport':'can_inspect_ssl_transport','gss_transport':'can_inspect_gss_transport'}.get(name)
            if transport_capability and not identity[transport_capability]:
                sections[name] = dict(status='unavailable',reason='Transport metadata SELECT privilege unavailable.',rows=[],truncated=False)
                continue
            try:
                sections[name] = inspect_query(connection,sql,catalog_parameters,args.max_rows)
                if transport_capability and not sections[name]['rows']:
                    sections[name].update(status='unavailable',reason='Server transport row unavailable; pooler/visibility requires review.')
            except psycopg.Error as exc:
                # No SAVEPOINT/ROLLBACK TO commands. A failed catalog query
                # aborts the transaction; stop rather than resuming permissively.
                state = exc.sqlstate if re.fullmatch(r'[0-9A-Z]{5}',exc.sqlstate or '') else None
                sections[name] = dict(status='unavailable',sqlstate=state,rows=[])
                report['sections'] = sections
                report['incomplete'] = True
                report['unattempted_sections'] = [key for key in QUERIES if key not in sections]
                print(json.dumps(report,ensure_ascii=True),flush=True)
                raise DiagnosticError('Catalog inspection stopped after a query failure; details and credentials suppressed.') from None
        progress['stage'] = 'assessment'
        report['sections'] = sections
        report['findings'], report['runtime_compatibility'] = assess(sections,identity,args.schema)
        report['incomplete'] = any(s['status']!='ok' or s.get('truncated') for s in sections.values())
        # Roll back even the successful read-only transaction; never persist a session change.
        progress['stage'] = 'rollback'
        connection.rollback()
    progress['stage'] = 'report_output'
    print(json.dumps(report,ensure_ascii=True),flush=True)
    return 2 if report['incomplete'] else 0


# Only fixed labels are emitted. Never classify by inspecting exception text.
FAILURE_CATEGORIES = {
    'preflight': 'preflight_failed', 'authorization': 'authorization_input_failed',
    'driver_import': 'driver_unavailable', 'credential_input': 'secure_input_failed',
    'url_parsing': 'url_parse_failed', 'connection_configuration': 'connection_configuration_failed',
    'tls_configuration': 'tls_configuration_failed',
    'connection_open': 'connection_tls_auth_or_startup_failed',
    'transaction_configuration': 'transaction_configuration_failed',
    'identity_query': 'identity_query_or_result_failed',
    'read_only_verification': 'read_only_verification_failed',
    'startup_verification': 'startup_verification_failed',
    'database_verification': 'database_verification_failed',
    'runtime_role_verification': 'runtime_role_verification_failed',
    'identity_output': 'output_failed', 'catalog_inventory': 'catalog_inventory_failed',
    'assessment': 'assessment_failed', 'rollback': 'rollback_failed',
    'connection_cleanup': 'connection_cleanup_failed', 'report_output': 'output_failed',
}


def run_cli(argv=None):
    progress = {'stage': 'preflight'}
    try:
        return main(argv, progress)
    except DiagnosticError as exc:
        # DiagnosticError messages are fixed strings from the reviewed source.
        print(str(exc),file=sys.stderr)
        category = 'safety_check_failed'
    except (Exception,KeyboardInterrupt) as exc:
        # Connection/parser/driver errors can contain complete credentials.
        category = 'interrupted' if isinstance(exc,KeyboardInterrupt) else FAILURE_CATEGORIES.get(progress['stage'],'unexpected_failure')
    stage = progress['stage'] if progress['stage'] in FAILURE_CATEGORIES else 'unknown'
    cleanup = ' Cleanup also failed; details suppressed.' if progress.get('cleanup_failed') else ''
    print(f'Diagnostic stopped: stage={stage}; category={category}. Sensitive error details suppressed. No writes were requested.{cleanup}',file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(run_cli())
