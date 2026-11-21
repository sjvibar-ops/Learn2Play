# Learn2Play catalog-only PostgreSQL security diagnostic

**Source-reviewed and offline-tested with fake connections only. No standalone diagnostic invocation, real database connection or migration execution has occurred. Any real connection/inspection still requires separate approval.**

The standalone tool is `scripts/audit_database_security.py`. It leaves the older PowerShell diagnostics unchanged. It imports no Learn2Play application modules, loads no dotenv files, performs no migrations, and changes no roles, RLS, grants, persistent configuration or environment variables. It creates no report file; authorized runs print JSON metadata to standard output. No new dependencies are required: Psycopg 3 is already declared by the project.

## What it inventories

The application table list includes accounts, applications, bookings, messages, reports, scheduling, XP, attendance, payments, and the financial tables ledger_accounts, ledger_transactions, ledger_lines, and admin_audit_events. It also inspects alembic_version **metadata only**. Similarly named tables in other non-system, non-managed schemas are reported as candidates. Missing expected tables in the selected application schema are reported separately from incomplete output.

| Section | Checks |
| --- | --- |
| Identity | Actual database, current schema/effective schemas, current/session/session-authorization roles, PostgreSQL version, replica status, server-enforced read-only state, diagnostic isolation and configured default isolation/row_security setting and session_replication_role. |
| Transport | Current-backend pg_stat_ssl protocol/cipher/bits and pg_stat_gssapi authentication/encryption flags, when SELECT metadata is available. No principals, certificate identities or application traffic are read. Behind a pooler these describe its PostgreSQL-facing leg, not necessarily client TLS. Missing privileges/rows are explicitly unavailable and make collection incomplete. |
| Roles | SUPERUSER, BYPASSRLS, CREATEDB, CREATEROLE, LOGIN, INHERIT, REPLICATION; connection membership, immediate inherited rights and possible SET-role capability. No role passwords are selected. |
| Role/database settings | Only allowlisted isolation/read-only/row_security/search_path metadata. Only known isolation/boolean values are printed; search_path values and arbitrary setting literals are omitted. Global and current-database entries are included. |
| Memberships | Granted/member/grantor roles and admin option; per-membership inherit/set options where available. PostgreSQL versions before 16 show null for the newer option columns. |
| Tables | Owner, RLS/FORCE, policy counts, current-connection RLS activity and inherited owner privileges. |
| Policies | Names, commands, permissive/restrictive status and assigned roles, including PUBLIC. Expression presence and literal-true flags are reported; expressions themselves are never printed. |
| Table/column ACLs | Expanded grants and grant options, including PUBLIC and grantors, plus effective SELECT/INSERT/UPDATE/DELETE and other privileges for anon, authenticated, service_role, current role and session role where those roles exist. Column names/types/flags are metadata, not values. |
| Schemas | Non-system schema owners, ACLs, effective USAGE and CREATE for the relevant roles. |
| Sequences | Sequences in the requested application schema plus owned/default-referenced sequences associated with target tables, including associations outside that schema. Owners, identity/default associations, ACLs and effective USAGE/SELECT/UPDATE are reported. Serial/default references are distinguished from identity ownership, whose permission requirements can differ. No sequence values are read or advanced. |
| Default ACLs | Creator-role global/schema entries, grantees, grantors, object kinds and grant options, retaining explicit empty ACL entries. Absent entries do not mean that built-in/platform defaults grant nothing. |
| Views | Non-system views/materialized views, owners, security-invoker/barrier flags, direct relation dependencies; table and column-level SELECT/INSERT/UPDATE and table DELETE ACL paths. Separate view ACL, column ACL and effective column-privilege sections identify the affected columns and grantors. ACLs do not establish view updatability or materialized-view write capability. No definitions or view contents are queried. |
| Functions | Non-system functions/procedures and other pg_proc metadata: identity signature without default argument values, result type, owner, SECURITY DEFINER, existence of a search_path setting, ACLs and effective EXECUTE. Bodies, binary paths and setting values are excluded. |
| Triggers | Target-table trigger names, enable modes and function identities; expected payment-event and provider-history guard names, functions/schema, event/timing/type bits, origin enable modes and absence of WHEN clauses. Replica trigger mode is flagged (local behaves like origin for these guards). This metadata does not prove the function bodies or resistance to privileged tampering. Also inventories event-trigger metadata, without definitions or invoking them. |
| Storage | Catalog-only metadata for storage.objects and storage.buckets and their policies/ACLs when present. Bucket rows, privacy flags, receipt/object paths and file contents are not read. |

Views/functions/default ACLs/schema metadata deliberately include non-system objects beyond the named tables, since cross-schema wrappers can expose application data. This is a conservative metadata superset; it is not a statement that every discovered object is an application object or exploitable endpoint.

## Safety and connection controls

- The program defaults to refusing connection; `--allow-connect` and an interactive `APPROVE READ ONLY` confirmation are both required.
- Explicit expected TCP host, port and database must match the parsed credential target before connecting. A service name, hostaddr override, multiple-host target, supplied startup options, replication mode and target_session_attrs are rejected. The validated host/port/database are passed explicitly to libpq. Ambient PGSERVICE/PGSERVICEFILE/PGHOSTADDR/PGOPTIONS overrides are rejected, not cleared, to prevent hidden target/startup changes. There is no hardcoded project or postgres-role assumption.
- The credential comes from an echo-protected prompt, or from an existing DATABASE_URL only with explicit `--use-database-url`. Nothing assigns to the environment. Do not put credentials in command-line arguments, terminal logs or output files. Error handling suppresses parser/connection/driver details and tracebacks; SQLSTATE alone can identify a failed catalog stage.
- TLS hostname verification is mandatory: omitted sslmode is pinned to verify-full; every other supplied mode, including require and verify-ca, is rejected. gssencmode is explicitly pinned to disable, overriding ambient defaults; conflicting supplied GSS modes are rejected. verify-full requires an appropriate trusted root certificate configured through normal libpq mechanisms. Certificate/key paths are not printed. libpq validates the client-facing TLS endpoint; server transport metadata may describe a different pooler hop.
- Startup options require default_transaction_read_only=on and search_path=pg_catalog. The first identity query checks that this lookup path was established before any inventory; a mismatch stops. The fixed catalog lookup path prevents user-schema operator/type lookup from changing these queries; current_schema/effective_schemas describe the diagnostic context, not the original Flask search_path. The --schema argument still selects the application schema to audit. Psycopg also starts an explicit read-only REPEATABLE READ transaction. The first identity SELECT verifies both read-only settings and the approved database before any inventory query. There is **no read-write fallback** if a server/pooler rejects or strips required options. Prepared-statement auto-preparation is disabled for pooler compatibility.
- Database/role identity is printed before further inspection. `--expected-runtime-role` can additionally stop an accidental inspector-role run; this expects the SQL role, which may differ from a project-scoped pooler login string. Omitting it does not establish that the connection matches the actual Flask runtime.
- All inventory statements are fixed catalog SELECT/WITH queries with bound parameters. No application-table rows, function/view bodies or raw policy expressions are selected. A regex statement guard, narrow relation-name check and built-in function allowlist catch common accidental additions. They are not a general SQL parser, do not handle every quoted identifier/comment/operator/cast form, and do not guarantee arbitrary SQL is safe. Every fixed-query change requires source review; user-supplied SQL is never accepted.
- Database-level read-only mode is the enforcement boundary, not just a Python promise. Driver transaction control (BEGIN READ ONLY/ROLLBACK) is required; inspection SQL contains no DDL, DML, role switching, nextval, setval or modifying functions. The successful transaction is rolled back too. Read-only mode alone is not a sandbox for arbitrary SQL, so the catalog/function restrictions remain necessary.
- Per-statement timeout is 15 seconds, lock timeout 2 seconds, idle-transaction timeout 60 seconds and libpq connection timeout 10 seconds per address attempt. These are not a total client wall-clock deadline. Server-side row limits bound each section's outer row count, not all server work, nested arrays or JSON byte size; truncation is explicit. The default cap is 20,000 rows per section, configurable up to 100,000.
- A catalog permission/query failure produces available partial metadata with an incomplete marker and stops. The tool does not recover with a broader role or open a new connection. Known missing transport-view SELECT access is detected before querying that view and reported unavailable without an error; an unexpected SQL error still stops the transaction. A failed/truncated section is never evidence that missing policies/grants/objects are safe.

## Future invocation, only after approval

Use the existing virtual environment, a compatible PostgreSQL 12+ endpoint and the credentials used by the intended Flask runtime when evaluating runtime compatibility. No command below has been run.

```powershell
.venv/Scripts/python.exe -B scripts/audit_database_security.py `
  --allow-connect `
  --expected-host APPROVED_HOST `
  --expected-port 5432 `
  --expected-database APPROVED_DATABASE `
  --schema public
```

Replace only the non-secret target placeholders. For a transaction-pooler endpoint, specify its actual approved port (often 6543). The tool then displays the approved target, requests explicit consent, and securely prompts for the PostgreSQL URL/conninfo. It accepts the project's PostgreSQL URL variants, including postgresql+psycopg, without importing Flask or exposing the URL.

If separately approved and the existing process already has the correct DATABASE_URL, add `--use-database-url` instead of entering the URL. This reads that value and never changes it. Do not print it to verify it. Optionally add `--expected-runtime-role APPROVED_SQL_ROLE`; a mismatch stops before inventory. Keep JSON output private: role, object and privilege metadata can itself be sensitive even though credential/application values are excluded.

Exit 0 means a complete metadata collection, **not a security pass**. Exit 2 means no authorization, invalid configuration, a failure or incomplete/truncated output. Security findings do not automatically change permissions or provide a deployment authorization.

## Runtime compatibility interpretation

The required-access matrix follows the current direct-SQL Flask architecture: users/appointments/role_applications/messages/reports/payments require SELECT/INSERT/UPDATE; credentials and xp_events/attendance_events/payment_events/role_application_history require SELECT/INSERT; availability/unavailable dates require SELECT/INSERT/DELETE. Runtime DDL, audit-table mutation, sequence UPDATE, migration-state writes and broad role attributes are flagged for review. Actual column grants can require more detailed review rather than an automatic incompatible verdict.

The report surfaces BLOCKED runtime-operation assessments as HIGH RUNTIME_OPERATION_BLOCKED findings, while still requiring verification of the actual Flask SQL identity. It distinguishes missing ACL/schema/sequence access, missing applicable permissive RLS policies, policy-expression review and apparently possible ACL access. Applicable policies are matched using role inheritance, not just exact role names. RLS does not restrict TRUNCATE/REFERENCES/TRIGGER/MAINTAIN; grants for those are reported as SQL/RPC capabilities rather than hypothetical REST endpoints. MAINTAIN is checked only on PostgreSQL 17+. Replicas cannot be treated as write-capable runtime targets. The source booking mutex requires READ COMMITTED, so a different configured default isolation is flagged; the diagnostic's own REPEATABLE READ snapshot is deliberate and does not change Flask.

Flask-Login identities are not automatically Supabase Auth identities. The tool does not recommend blindly forcing RLS, adding auth.uid() policies, granting broad client access or giving the runtime BYPASSRLS. The service_role API/Storage key and DATABASE_URL login are distinct concepts.

## Limits

- This diagnostic cannot prove absence of every exposure. It reads a metadata snapshot, not application records or real client requests.
- Policies may be restrictive or semantically unsafe despite having expected roles; expressions are intentionally omitted to avoid exposing embedded literals. Function/view bodies, dynamic SQL, foreign integrations and arbitrary secondary data paths require separately authorized review.
- A visible/active guard name does not prove its implementation has not changed. No trigger function is invoked or body evaluated.
- Data API enabled/exposed-schema settings, actual Storage bucket privacy, client credentials and external consumers are not established from these catalogs. Correct Storage policy metadata alone cannot prove private receipt isolation.
- Discovery covers the named base relations and related objects, not every unknown/renamed table or partition child. A missing relation/role is not automatically evidence of a secure deployment.
- Missing pg_default_acl rows do not rule out built-in defaults, creator-specific behavior or platform/event-trigger automatic grants. Effective grants must be interpreted with role attributes, memberships, RLS and exposure settings.
- Diagnostic read-only/timeouts/isolation/search_path differ intentionally from Flask. Source connection overrides, transaction-pooler behavior and runtime operation success remain untested. No application operation or DML probe is attempted, even inside a rollback.
- An owner/superuser/compromised server can undermine security controls. The tool is not a cryptographic audit or an authentication mechanism for financial account-holder confirmations.

## Local review performed

Offline tests load reviewed definitions from AST with the standalone __main__ block excluded, use a local conninfo parser, and replace every connection and SQL result with test doubles. They exercise consent gates, target/SSL/GSS checks, read-only setup order, rollback, sanitized failures, truncation, fixed query safeguards, view exposure and guard metadata. They do not launch the diagnostic CLI (including --help), read real credentials, execute SQL or contact a database. SQL execution compatibility and actual permissions remain unverified. Application code, migrations, environment and secrets are unchanged in this preflight.


Implementation references: [PostgreSQL catalog/privilege functions](https://www.postgresql.org/docs/current/functions-info.html) and [Psycopg 3 transaction characteristics](https://www.psycopg.org/psycopg3/docs/basic/transactions.html#transaction-characteristics). These public documents were consulted without connecting to a project/database.

## Flask SQLAlchemy runtime permission contract

The actual SQL login, current_user, session_user, memberships and ownership are **unknown offline**. Do not assume postgres, or equate the backend SUPABASE_SERVICE_ROLE_KEY with the DATABASE_URL login or the SQL role named service_role. A pooler login string may differ from the SQL role. A separately approved inventory should use the real Flask connection identity and, once known, --expected-runtime-role to detect mismatches.

The current application uses one shared SQLAlchemy backend identity and Flask-Login users stored in public.users. Its authorization is implemented by authenticated routes, assigned-participant checks and admin checks. It does not establish Supabase Auth request claims or switch database roles per Flask user. auth.uid() policies cannot be assumed to work. SQL privileges alone do not enforce student/mentor/admin HTTP permissions.

| Tables | Current operational needs |
| --- | --- |
| users | SELECT for authentication/profile/roles; INSERT for student signup; UPDATE for profile/password/deactivation/admin flags and reviewed provider-role grants. Scheduling also locks stable user rows. Password hashes are backend-only data. |
| appointments | SELECT/INSERT/UPDATE for booking, cancellation, session eligibility, attendance and lifecycle snapshots; row locks must work. No normal DELETE/TRUNCATE. |
| role_applications | SELECT/INSERT/UPDATE for per-role submission/resubmission and admin review, locking the applicant User before the application. |
| credentials | SELECT/INSERT, including server-only storage-path reads and application ownership. No normal UPDATE/DELETE; failed uncommitted inserts are rolled back, while object cleanup uses Storage. |
| role_application_history | INSERT for submission/admin decision snapshots and generated-ID RETURNING; SELECT on returned/generated fields and any separately authorized audit reader. Full history SELECT is not a general client entitlement. No UPDATE/DELETE/TRUNCATE. |
| messages, reports | SELECT/INSERT/UPDATE for messaging/read state and admin report review. |
| mentor_availability, mentor_unavailable_dates | SELECT/INSERT/DELETE; schedule/user locking and conflict checks remain required. |
| payments | SELECT/INSERT/UPDATE for receipt revisions, verification, duplicate-transfer allocation and reconciliation. |
| xp_events, attendance_events, payment_events | SELECT/INSERT for progress/leaderboard, attendance and authorized audit/receipt reads. No normal mutation or deletion of events. |
| alembic_version | Normal HTTP requests require no access, particularly no writes. Migration administration uses a separately authorized deployment identity. |

This is a table-level operational checklist, not executable GRANT SQL or proof that every column needs broad rights. The diagnostic conservatively marks partial column grants REVIEW_COLUMN_GRANTS; verify actual ORM SELECT lists, INSERT RETURNING fields and UPDATE columns before approving a narrower column contract. SQLAlchemy commonly selects every mapped column when loading a model. SQL row locking requires appropriate SELECT/UPDATE rights; serial-backed inserts need associated sequence USAGE (UPDATE is more powerful and normally unnecessary). SELECT on a sequence is not a substitute for nextval permission. Identity-generated columns can differ. No generator or DML probe is used by this diagnostic.

The runtime needs CONNECT to the intended writable database, USAGE on the application schema and the operational ACLs above. Its required sequence access is separate from table privileges. PostgreSQL schema ownership/CREATE, table ownership, CREATEROLE, CREATEDB, REPLICATION, SUPERUSER, BYPASSRLS, TRIGGER, TRUNCATE, MAINTAIN and broad SET ROLE authority are not normal Flask-request requirements. REFERENCES and migration DDL are not runtime requirements either. Do not grant these to make a failed test pass. Owning a table is distinct from table ACLs: owners can change/regrant permissions and usually bypass non-FORCE RLS even where an ordinary SELECT grant was revoked.

For any RLS-protected table, the real runtime needs an established effective access path: an ordinary non-owner role requires appropriate applicable policies **and** SQL grants. No applicable policy means default deny; INSERT/UPDATE/RETURNING may also depend on SELECT policies. An owner without FORCE RLS or a superuser/BYPASSRLS role may currently work but carries authority to review, not a recommended workaround. A shared backend-only policy design requires deliberate approval; never blindly add auth.uid(), FORCE RLS, permissive client policies or BYPASSRLS. Memberships, inherited owner rights, SET ROLE paths and restrictive policies must be considered.

Scheduling requires READ COMMITTED in the application and a writable primary. The diagnostic deliberately uses read-only REPEATABLE READ with catalog-only search_path. Original application schema resolution is therefore unverified. Startup options mask the original read-only default, so the diagnostic's default_read_only=on is not evidence the Flask connection would be writable. Role/database setting metadata helps but cannot prove original server defaults or application success. Trigger execution should normally use origin mode (PostgreSQL treats local like origin for ordinary trigger firing); privileges allowing session_replication_role changes or trigger disabling need separate review. Metadata of the current setting does not prove absence of such authority.

Supabase Storage calls use backend service credentials through the Storage API. That does not require granting the SQLAlchemy login direct access to storage.objects or storage.buckets, and does not make service_role the Flask SQL login. Credentials/receipts require private buckets, server-only service credentials, admin/owner authorization on download routes, attachment handling and tested failure/cleanup behavior. Avatar policy is separate. This catalog diagnostic reads no bucket rows, paths, credentials or file data, so actual bucket privacy, Storage policies' semantics and live SDK authorization remain unverified.

Default ACLs affect future objects created by the listed creator role, not existing object grants, and are not simply inherited from every parent role. Schema-specific defaults add to global defaults. No stored row means built-in defaults may still apply (for example PUBLIC EXECUTE on routines). Explicit empty entries are retained in output (empty table/schema/sequence/function ACL expansions retain their object row with null grant fields); interpret them with global/schema scope and the object kind, not as universal denial.

## Provider migration source review and rollout gate

Reviewed source: migrations/versions/a6d2f9c83b10_provider_applications_and_history.py. Its parent is f4c2a8d91e70. Static graph inspection finds 13 unique revisions, one root (0720c7d1e4ca), one head (a6d2f9c83b10), all parents present, no cycles and the existing e6a1c9d84b20 merge intact. These are repository facts, not verification of the remote schema or alembic_version contents. Earlier committed/uncommitted migration files are preserved; the new revision has a fresh identity instead of replacing the source branch revision c4a7d91e5b26.

- **HIGH / unresolved rollout compatibility:** upgrade enables RLS on the new history table and defines no runtime policy or runtime grants. A non-owner runtime subject to RLS cannot append history without a separately established policy/ACL path. The runtime identity/ownership is unknown; do not presume it works or grant BYPASSRLS. Prior xp_events/attendance_events/payment_events protection has the same need for actual-role verification.
- **MEDIUM / schema prerequisite:** table/function creation and FK targets are unqualified, while RLS/revocations explicitly target public.role_application_history and its conventional serial sequence. Verify the migration role's effective search_path, actual application schema, sequence identity and absence of conflicting objects on a disposable PostgreSQL database before rollout. One Alembic head does not establish these prerequisites.
- **MEDIUM / semantic integrity limit:** FK targets and application/user indexes exist, but actor_id is nullable; role/event/status fields have no database CHECKs; independent FKs do not ensure the history user/role matches its application. There is no event-revision uniqueness constraint. HTTP code derives these values and serializes transitions under locks, but an INSERT-capable SQL identity can still forge inconsistent or duplicate events. Append-only does not imply authentic or semantically valid history.
- **MEDIUM / ACL coverage limit:** revocations name PUBLIC, anon and authenticated and the sequence. They do not remove arbitrary existing/default grants to other roles, inherited ownership or BYPASSRLS. No FORCE RLS is applied. Review effective privileges/default ACLs for all relevant creator/consumer roles; do not infer universal privacy from those revocations.
- **Audit guard scope:** PostgreSQL defines BEFORE UPDATE OR DELETE row guards and a BEFORE TRUNCATE statement guard; SQLite defines UPDATE/DELETE guards. They reject ordinary mutation while enabled. Owners/privileged administrators can disable/drop/replace guards; replication-mode behavior and function bodies still need verification. No cryptographic authenticity or owner-proof retention is claimed.
- **Record preservation / rollback:** submitted_at and credential application_id are nullable additions. Legacy evidence stays unassigned; no application records are backfilled or deleted. The history table has application/user/actor FKs and indexes on application/user IDs; credentials gets a named application FK/index. Existing per-user/per-role uniqueness is retained. Downgrade deliberately raises before dropping anything, so operational rollback requires an approved forward repair/retention plan rather than an automatic downgrade. No migration function was run.

Before rollout, separately approve: (1) upgrade from a representative prior schema on a disposable PostgreSQL database, verify all old rows/constraints/guards and ORM operations; (2) real PostgreSQL submission/review/booking/attendance lock and concurrency tests; (3) actual runtime and migration role identities, memberships, grants, schema resolution, sequences and RLS compatibility; (4) private receipt/credential buckets and authorized/unauthorized download behavior with mocked tests followed by separately authorized service verification; (5) audit retention, owner/trigger authority and failure/orphan-reconciliation procedures. No remote database or Storage configuration has been inspected or changed in this task.

Additional primary references: [PostgreSQL libpq TLS/GSS parameters](https://www.postgresql.org/docs/current/libpq-connect.html), [PostgreSQL 12 transport-statistics views](https://www.postgresql.org/docs/12/monitoring-stats.html), [PostgreSQL RLS defaults and owner behavior](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).

PostgreSQL distinguishes replica trigger mode from origin/local; see [client connection defaults](https://www.postgresql.org/docs/current/runtime-config-client.html). No permission to change that setting is established by reading its current value.

The earlier source-only concern about the other nine private application tables remains: the current migration chain does not define equivalent RLS/ACL protections for them. Actual grants, memberships and Data API exposure are unknown, so this is a conditional HIGH rollout concern rather than proof of a reachable client leak. Hardening this diagnostic does not repair database permissions.

### Complete local revision-parent map (AST inspection only)

| Revision | Parent(s) |
| --- | --- |
| 0720c7d1e4ca | None (root) |
| 9e45f7e7aa32 | 0720c7d1e4ca |
| c1a82b7d43ef | 9e45f7e7aa32 |
| c4b8a29f71d2 | 9e45f7e7aa32 |
| f31a6e8c9b24 | c4b8a29f71d2 |
| d2b7a4e9310c | f31a6e8c9b24 |
| e6a1c9d84b20 | c1a82b7d43ef, d2b7a4e9310c |
| a8f2b6c01d73 | e6a1c9d84b20 |
| b9d3e7f02c84 | a8f2b6c01d73 |
| c7e4a1b92d60 | b9d3e7f02c84 |
| e9b7c2d41a60 | c7e4a1b92d60 |
| f4c2a8d91e70 | e9b7c2d41a60 |
| a6d2f9c83b10 | f4c2a8d91e70 |
