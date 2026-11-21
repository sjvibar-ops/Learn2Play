# Provider applications integration

Selective source: origin/updated at 70bd5e044ac56618b5b922388fead58dd3a6ac52.

## Security and schema design (reviewed before implementation)

Registration creates only a student; submitted role/admin fields cannot grant privileges. Active legacy users with no roles can log in to apply, but provider permissions still require an approved role. Existing roles are not backfilled or silently changed.

Applications are per-user/per-role. Legacy credentials keep application_id NULL, are identified as legacy evidence, and are never guessed into an application. Only admins download evidence; applicants see their own safe metadata. Mentor/coach application submission cannot approve an application or grant roles.

Submission and review must lock the applicant User before the RoleApplication, refresh locked state, and commit application changes, evidence metadata, history and role grants atomically. Storage uploads must finish outside database transactions; conflicts/failures clean up only newly generated paths. Ambiguous commit outcomes require preservation/reconciliation rather than deleting possibly committed evidence.

New additive revision a6d2f9c83b10 follows f4c2a8d91e70. It adds submitted_at, nullable credential application ownership and role_application_history. History retains prior application state plus actor and decision notes. ORM guards are supplemented by database UPDATE/DELETE guards and a PostgreSQL TRUNCATE guard. PostgreSQL RLS and revocation of PUBLIC/anon/authenticated table and sequence access define restrictive defaults for those client roles (all memberships/default ACLs still require review); no auth.uid() policy or FORCE RLS is assumed appropriate for Flask-Login. The actual backend runtime must have narrowly scoped SELECT/INSERT rights and an appropriate RLS access path, reviewed separately before rollout. Owners/superusers can defeat guards; these are not owner-proof.

No migration is executed in this task. Existing production schema is incompatible with the new model until an independently approved migration rollout. History downgrade is refused to prevent silent audit loss. Existing migration files remain unchanged.

## Storage and validation

Use the existing backend-only Supabase credentials bucket, kept private by deployment configuration. Preserve generated object paths, allowlisted PDF/JPEG/PNG extensions, signatures and 5 MiB/file limits; validate JPEG/PNG through the existing bounded decoder. Up to five evidence files per application; links and experience are bounded. PDF signature checks are not malware/content scanning. Credential downloads remain attachments, admin-only. Existing global 32 MiB request limit applies before CSRF parsing.

## Scope

No Discord, directory reorganization, changes to payment/attendance/XP/booking/JaaS policy, remote database access, environment changes or deployment. Tests use isolated in-memory SQLite and mock Storage. PostgreSQL concurrency and the migration itself need separately authorized disposable-database verification; SQLite does not prove row-lock behavior.

## Rollout and operational limits

The adapted revision deliberately has a fresh ID rather than reusing the source branch revision with a different parent/body. Its target is the existing public PostgreSQL schema. Keep the prior migration files unchanged. No downgrade silently drops evidence or history.

Apply/resubmit is available for each role independently in Settings > Roles & access. Experience: 50–1500 characters; up to five HTTP(S) links, 300 characters each. Evidence is optional. Revision notes require 10–500 characters; rejection notes remain optional, up to 500 characters. History records submissions and admin decisions; no historical decisions are invented for legacy rows. Existing credential application ownership remains unknown for legacy evidence.

Generated evidence paths explicitly disable upsert. Failed uploads are cleaned up best-effort. On an ambiguous commit, positively referenced evidence is retained; if the database cannot establish the result, cleanup is deferred and requires operator review. There is no automated orphan-cleanup worker in this stage.

Extend the separately reviewed database diagnostic to cover role_application_history before a future comprehensive remote inventory; that script is intentionally unchanged in this task. PostgreSQL RLS/ACL runtime compatibility, actual private bucket settings and PostgreSQL locking still require separately approved verification. PDF checks are not malware scanning. No new dependencies were added.

Academic/gaming learning preferences remain browser-local at signup and in Settings. They never grant provider roles; teach/coach intent is now handled by the application workflow. Legacy browser preferences are retained.
