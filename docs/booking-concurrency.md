# Atomic booking reservations

Booking creation previously queried for overlaps and then inserted an Appointment without a shared reservation lock. Two transactions could both observe an empty slot, pass the check, and each create an appointment/payment. Locking only appointments cannot fix an initially empty schedule.

## Database coordination

`scheduling.lock_schedules` acquires PostgreSQL `FOR NO KEY UPDATE` locks on both participants' existing User rows, one ID at a time in ascending numeric order. Each authenticated student/provider has a stable row before any booking exists. Separate requests and application workers targeting the same person therefore serialize inside PostgreSQL. Disjoint participant pairs proceed independently; different times for the same person serialize briefly but can both succeed.

NO KEY UPDATE conflicts with another reservation lock while permitting the KEY SHARE locks used by foreign-key inserts (including payment/attendance audit records). User IDs are not changed. Numeric ordering handles reversed student/provider roles without a lock-order cycle. The helper refreshes locked user state so provider roles, active status and server price are rechecked after waiting.

After both locks are held, create_booking reads current availability, unavailable dates and active appointments, checks overlap, inserts the appointment, creates the associated payment if required, and commits. Appointment/payment failures roll back the entire operation. Validation/conflict responses release the transaction via rollback. No payment is created on a losing request. Normal slot conflicts return 409 with a request to choose another slot; database failures/timeouts return a generic retryable 503 without internal details.

The protocol requires PostgreSQL READ COMMITTED isolation: the overlap query after waiting must see the prior lock holder's committed reservation. The application checks actual transaction isolation and fails closed with 503 for other PostgreSQL isolation settings, including REPEATABLE READ. It does not change database/session isolation or environment variables. SQLite supports the existing sequential functional suite but does not provide these PostgreSQL row-lock guarantees; concurrent SQLite booking is not a supported production configuration.

## Scheduling rules retained

- New bookings are 60 minutes, use whole-hour Philippine local wall times, and store naive UTC timestamps after subtracting eight hours.
- Sessions must fit fully within a same-day availability window, be in the future and within the existing 60-day horizon. The existing availability API permits same-day windows with end after start, not overnight windows. A timezone suffix on the submitted wall time is rejected rather than interpreted ambiguously.
- pending, accepted and scheduled appointments reserve time. cancelled, rejected and completed appointments do not reserve future slots under the existing policy.
- Intervals are half-open: [start, end). 10:00-11:00 and 11:00-12:00 can both be booked. Existing appointments use their stored duration, with the existing 60-minute fallback.
- A person's learning and teaching appointments share one schedule. Conflict checks inspect both participant columns for both people. Availability previews use the same participant scope; previews are advisory and final reservation always rechecks under locks.
- Availability and unavailable-date mutations acquire the provider's same schedule mutex. If an edit commits first, the waiting booking sees it. If the booking commits first, later availability edits retain the existing behavior of not automatically cancelling that booking.

## Acceptance and existing data

Pending appointments already reserve their slots. Acceptance additionally acquires both participant locks, then locks/reloads the appointment and checks other active reservations before assigning accepted status/meeting room. This prevents accepting conflicting requests left by the old implementation. If legacy requests conflict, acceptance returns 409; the provider must explicitly reject/cancel the appropriate request through the existing workflow before retrying.

Cancellation and rejection retain their existing authorization and statuses. They remove a reservation rather than adding one, so they need no new schedule mutex. A create request racing a cancellation can conservatively return 409 while the cancellation is uncommitted; retry after cancellation commits. Attendance and payment handlers retain their existing locks and eligibility rules. No payment verification, attendance, XP or JaaS restriction was relaxed.

Existing conflicting rows are preserved, not automatically repaired or deleted. In particular, already accepted legacy conflicts require human review; this fix does not choose a winner or refund money.

## Scope and deployment limitations

No schema change, extension, migration, new dependency or environment variable is required. The sole Alembic head remains c7e4a1b92d60. No migration or Supabase operation was executed.

This is a cooperative database lock protocol for trusted booking writers, not an exclusion constraint against arbitrary SQL inserts. Future rescheduling, imports, alternate APIs and scripts must take the same participant locks before checking/writing reservations. Direct database/Data API permissions still need the separately documented security review. The database role must be allowed to lock both participant User rows. Keep all work local until deployment is separately authorized.

## Tests and reproduction

The PostgreSQL suite uses a new disposable loopback-only cluster, separate real database connections and transactions. Test timing wrappers delegate to the actual lock helper; locks are never mocked away. Tests hold the first transaction's acquired locks, verify that a competing shared-resource request waits, and check distinct backend connection IDs. Disjoint-resource tests verify that the second reservation can finish while the first remains held.

Coverage includes provider contention, student contention across providers, adjacent/nonoverlapping slots, independent providers at the same time, reversed learning/teaching roles, and fail-closed unsupported isolation. SQLite functional tests cover reserving/nonreserving statuses, legacy acceptance conflicts, availability edits, unavailable dates, free bookings, stored durations, price snapshots, and rollback of appointment/payment INSERT failures.

Run the existing suite with a fresh task-owned pytest temporary directory if the machine's default temp directory has ACL restrictions. PostgreSQL tests require permission to start the disposable local cluster; they never read a remote DATABASE_URL or execute migration functions.

Files added: scheduling.py, tests/test_booking_reservations.py, docs/booking-concurrency.md. Files modified: routes.py, tests/test_attendance_postgres.py, docs/manual-payments.md, docs/gamification-attendance.md. Payment, attendance and gamification models/migrations and payment verification code remain unchanged by this follow-up.


## Executed validation

Final complete local suite: **160 tests passed, 37 subtests passed** in 317.36 seconds. This includes 18 new reservation functional/failure tests and seven new PostgreSQL tests (six actual two-connection reservation interleavings plus unsupported-isolation rejection). The PostgreSQL module now has 12 passing tests, retaining all five attendance/transfer concurrency tests from earlier work.

The first standalone PostgreSQL run passed all 12 tests. An initial combined run then encountered a disposable initdb startup timeout and a new fixture incorrectly expecting the unavailable-date POST response to contain an ID. The fixture was corrected to query the created record without changing the existing API; the disposable cluster initialization allowance was increased to 120 seconds for slow Windows startup. The final complete run passed without errors. No concurrency assertions were removed or weakened.

Git diff --check passed. Alembic inspection reports exactly one head, c7e4a1b92d60. No migration was created or executed. PostgreSQL tests used SQLAlchemy metadata in a newly initialized temporary loopback cluster, never the existing local application database or Supabase. No environment/secret changes, real funds, commit, push or deployment occurred. Live Supabase/Vercel/Render/JaaS and browser visual QA remain outside this local verification.

PostgreSQL locking reference: https://www.postgresql.org/docs/current/explicit-locking.html
