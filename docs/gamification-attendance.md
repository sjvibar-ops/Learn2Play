# Gamification, attendance and payment integration

Integrated locally from frontend commit `083651b89a69766d9b2ee1a267b8398b1b0935be` onto the existing `feature/video-calling` working tree at `22715cf1d988531514e439d5b3bdb2980eeaf653`. No merge, cherry-pick, branch switch, commit, push, deployment or Supabase access was performed. Existing payment code was reconciled at component level; shared files were not replaced with frontend copies.

## Normal workflow

1. An assigned educator accepts the existing booking. Payment-required sessions retain the existing VERIFIED payment requirement for both JaaS entry routes.
2. After the scheduled session end, each participant independently confirms attendance. The provider uses POST `/api/bookings/<id>/complete`; the student uses POST `/api/bookings/<id>/confirm-attendance`.
3. The first confirmation leaves the appointment accepted/scheduled and visibly waiting for the other participant. The second automatically finalizes the appointment and creates a single 50-point XpEvent if all eligibility checks pass.
4. Eligibility requires distinct assigned participants, an accepted/scheduled appointment, elapsed session end, both confirmations, no unresolved dispute or closed-without-XP outcome, and VERIFIED payment where required. Free/legacy-exempt bookings retain their exemption.
5. Repeated confirmations are idempotent. Existing completed appointments remain unchanged and are not retrospectively awarded XP. Admins do not confirm for participants or award XP.

No time-based, browser-activity-based or non-response-based attendance assumption exists. The Sessions page shows pending confirmations as an in-app reminder; no email, scheduled background reminders or outbound messages were added. Participants can remain pending indefinitely.

## Atomicity and races

`routes.update_booking` obtains a fresh `SELECT ... FOR UPDATE` appointment row before calling the attendance service. Confirmation timestamps, dispute checks, finalization, XP insertion and AttendanceEvent audit insertion share one database transaction and commit. Any SQLAlchemy transaction failure rolls back all related changes. A unique database constraint on XpEvent.appointment_id independently prevents duplicate awards. New self-bookings and same-person attendance confirmations are rejected.

Payment handlers retain their Payment -> Appointment lock order. Attendance only locks Appointment and reads payment status without acquiring a Payment row lock, preventing a reverse lock order. A payment approval waiting for attendance can proceed after that transaction releases the appointment; an unpaid attendance request must retry after verification.

Concurrent requests have a defined serial order: an accepted dispute committed first blocks later confirmations; finalization committed first rejects a later dispute with 409. There is never an accepted unresolved dispute alongside a new XP award. This flow does not revoke an already-finalized award or accept a post-finalization dispute. Such investigations require a separate policy/workflow.

SQLite functional tests verify transitions but cannot establish row-lock correctness. Dedicated tests start a newly initialized, disposable PostgreSQL cluster bound only to 127.0.0.1, use a fresh test database and SQLAlchemy metadata (not migrations), and shut down their own cluster. They never read DATABASE_URL or use an existing PostgreSQL/Supabase server.

## Disputes and admin decisions

Either participant may POST `/api/bookings/<id>/dispute-attendance` with a `note` of 5-1000 characters after the session ends, without payment verification. The reporter, UTC time and reason are recorded. New confirmations remain blocked until admin resolution. Repeated reporting of the same unresolved dispute does not duplicate events.

Admins use `/admin/attendance` (unresolved queue or resolved history) and POST `/api/admin/attendance/<id>/resolve` with `outcome` and a 5-1000 character `reason`:

- `reopen`: records the decision and admin/time, resets both confirmations, and requires fresh independent confirmation by both participants. Normal backend eligibility checks still apply; the admin action awards no XP. Cancelled/rejected appointments cannot be reopened by this flow.
- `close_without_xp`: records resolution and permanently excludes this appointment from XP. It does not cancel the booking, refund a payment or change the existing JaaS rules.

AttendanceEvent is an application append-only audit of participant confirmations, dispute reports, resolutions and XP creation. Previous confirmations and dispute reasons remain in this history even when the current attendance projection is reset. The admin UI exposes history in chronological ID order. No audit-edit/delete route is provided. Database owners retain SQL-level power; this is not a tamper-proof external ledger.

## Progress and leaderboard

GET `/api/student/progress` returns only the authenticated student's totals, levels, badges, fixed quests, weekly activity and balance streak. Mentor sessions award academic XP; coaching sessions award gaming XP. Levels start at 0, 100, 250, 500 and 1000 XP. Academic/gaming badges require XP in the respective path; combined requires both. Quests count three academic sessions, two gaming sessions, both paths in one week and the lifetime 100-XP milestone. No quest bonus XP or redeemable rewards exist.

The dashboard now uses backend data. Browser localStorage values are not imported or trusted. There is no API accepting XP totals, points, level or badge edits. XP becomes attributable to the week of the final confirmation/award, rather than necessarily the scheduled session week. Weeks run Monday-Sunday in Asia/Manila. A balance streak counts consecutive weeks with both paths, retaining the previous streak while the current week is still incomplete.

GET `/leaderboard` and GET `/api/leaderboard/weekly` show up to 50 active, non-admin, opted-in accounts ordered by backend weekly XP, then display name and ID. Responses expose rank, display name and weekly XP only. POST `/api/student/leaderboard-preference` accepts a boolean `opted_in` for the authenticated student only. Other submitted account IDs are ignored. All POSTs use global CSRF protection; progress/leaderboard responses are private/no-store. The frontend renders leaderboard names through textContent.

## Migration graph: files only, not executed

```text
0720c7d1e4ca -> 9e45f7e7aa32
                    |-> c1a82b7d43ef (existing manual payments) ------------------|
                    |-> c4b8a29f71d2 -> f31a6e8c9b24 -> d2b7a4e9310c -----------|
                                                        e6a1c9d84b20 (merge) <-|
                                                             |
                                                        a8f2b6c01d73 (audit/resolution)
                                                             |
                                                        b9d3e7f02c84 (PostgreSQL API restrictions)
```

The sole final head is `b9d3e7f02c84`. The merge revision has both payment and leaderboard heads as parents; no existing revision or parent was rewritten. Source migrations create xp_events, add nullable attendance timestamps/dispute reporter, and add leaderboard_opt_in with false default. The local audit migration adds nullable resolution fields plus attendance_events. Existing rows preserve their booking/payment access and opt out of the leaderboard by default.

Later, under separate authorization, inspect the actual database revision(s), backend SQL role and permissions, take a backup, and test the full upgrade on an isolated local copy. From either known branch head, Alembic can apply the other missing branch and advance through the merge to the single final head. Do not stamp, reset, guess the remote revision, or run the baseline against populated unmanaged tables. No migration command was executed for this task. Migration tests use AST/history inspection, PostgreSQL DDL compilation and fresh metadata-based constraint tests; they do not invoke upgrade/downgrade functions.

The original source XP/attendance/preference downgrade functions remain unchanged. The local payment, audit and security migrations refuse destructive/security-weakening downgrade operations. Plan forward repair and application rollback explicitly; do not assume a full downgrade is available.

## Supabase / deployment review still required

The final local migration enables RLS and revokes PUBLIC/anon/authenticated table and sequence privileges on xp_events and attendance_events on PostgreSQL. It is a no-op for those permissions on SQLite. This is fail-closed backend-only access: no browser-role policies are created. The migration has not been applied anywhere. The backend database role must be the table owner/BYPASSRLS or receive a deliberately reviewed backend policy and grants. A non-owner application role can otherwise be locked out. Do not grant browser roles write access to solve that error.

Remote grants, role inheritance, exposed schemas, existing users/appointments/payment-table API permissions and Storage policies were not inspected. These remain separate operator/security review items. The existing users table also stores leaderboard preferences and must not allow other accounts to update them through a direct Data API. Securing only the new XP table does not establish that the whole Supabase project is protected.

No new production environment variable is required for gamification. The Python runtime needs IANA timezone data for Asia/Manila (available in the tested environment); Windows distributions may need tzdata installed. Retain existing payment storage/QR/demo settings and backend-only Supabase/JaaS credentials. Render needs the reviewed schema before serving this application version; validate sessions, CSRF and uploads through Vercel's existing proxy before deployment. No secrets or environment files were modified.

The prior payment audit limitations remain: incomplete receipt decoding, whole-request upload limits, cancellation/payment reconciliation and payment-proof revision history need separate work. This integration preserves the payment implementation; it does not claim to repair those issues.

## Local tests

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

PostgreSQL concurrency tests use initdb/postgres/pg_ctl from PATH, or the installed PostgreSQL 18 binaries on Windows. If binaries are absent, those three tests explicitly skip; they must not be reported as PostgreSQL verification. The sandbox on this machine blocks initdb, so the test command needs permission to launch the disposable local server outside the sandbox. No production credentials or environment changes are needed. The tests only create/drop their own test tables and clean their own temporary cluster directory.

Source provenance: the three frontend migration files, leaderboard assets and initial gamification tests were taken from the specified commit; shared models/routes/UI were integrated selectively. Tests were extended for payment gates, both roles/orders, dispute reasons/resolutions/history, unauthorized actions, CSRF, rollback, legacy/free sessions, backend-derived progress, preferences and real PostgreSQL race ordering.


## Integration file inventory

Created by this integration:
- attendance.py
- migrations/versions/c4b8a29f71d2_add_verified_xp_events.py
- migrations/versions/f31a6e8c9b24_add_session_attendance_confirmations.py
- migrations/versions/d2b7a4e9310c_add_leaderboard_opt_in.py
- migrations/versions/e6a1c9d84b20_merge_payments_and_gamification.py
- migrations/versions/a8f2b6c01d73_attendance_audit.py
- migrations/versions/b9d3e7f02c84_secure_backend_progress_tables.py
- static/css/leaderboard.css
- static/js/leaderboard.js
- static/js/attendance-admin.js
- templates/student/leaderboard.html
- templates/admin/attendance.html
- tests/test_gamification.py
- tests/test_attendance.py
- tests/test_attendance_postgres.py
- tests/test_gamification_migrations.py
- docs/gamification-attendance.md

Modified by this integration: models.py, routes.py, static/js/script.js, static/js/sessions.js, static/css/styles.css, templates/_navbar.html, templates/student/student-dashboard.html, tests/test_payment_migration.py, docs/manual-payments.md.

Payment-only modules, configuration, app registration, booking redirect, payment UI/assets, payment migration and payment functionality tests remain unchanged from preflight. Their existing uncommitted Git status is retained. The migration test was revised to retain preservation/constraint checks without violating the instruction against executing migrations; reference and appointment uniqueness are now checked independently.


## Final executed validation

- Complete pytest run: **123 passed, 37 subtests passed** in 279.88 seconds, including the original regression suite, payment approval/rejection/privacy tests, all new attendance/admin/progress tests, migration graph/source/model checks, and three real PostgreSQL concurrency tests. No migration function was executed.
- JavaScript syntax checks passed for script.js, sessions.js, leaderboard.js and attendance-admin.js; git diff --check passed.
- Byte hashes confirmed all 12 payment-only components checked against preflight were unchanged. Shared booking/model/session/navbar components were reconciled rather than overwritten.
- The first full run after adding attendance tests passed 116 tests but hit four unrelated registration fixture setup errors on an old pytest temporary directory with restricted permissions. The successful final run used a fresh TemporaryDirectory as pytest's --basetemp; no existing directory was cleaned or reset.
- PostgreSQL tests used a fresh disposable PostgreSQL 18 cluster on 127.0.0.1 and shut it down after testing. Their initial sandbox run could not initialize the cluster; the separately authorized local run passed.
- Not performed: any migration execution, remote schema/permissions checks, live Supabase storage, live JaaS call, Vercel/Render deployment, or visual browser layout QA. These results establish local route, service, mock-UI and concurrency behavior rather than production deployment readiness.

If the machine's default pytest temp directory has ACL problems, use a newly created task-owned TemporaryDirectory and pass a new child path as --basetemp; do not delete/reset the existing pytest temp directory. This is how the final full suite was run.


## Follow-up payment allocation protection

The duplicate-transfer follow-up adds revision `c7e4a1b92d60` directly after `b9d3e7f02c84`. The current graph still has one head, now `c7e4a1b92d60`; the graph/head statements above describe the original gamification integration. See manual-payments.md for the allocation policy. No migration was executed by that follow-up.


## Follow-up atomic booking reservations

Booking creation and acceptance now coordinate provider/student schedules through ordered PostgreSQL User-row locks before checking overlaps. Appointment and payment insertion remain one transaction; a competing slot loser receives 409 without a new payment. Availability mutations share the same provider lock, and learning/teaching appointments share each person's schedule. No new migration is required; the head remains c7e4a1b92d60. See [booking-concurrency.md](booking-concurrency.md) for lock ordering, isolation requirements, tests and legacy-data limitations.
