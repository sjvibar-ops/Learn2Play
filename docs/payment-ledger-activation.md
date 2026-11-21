# Payment ledger activation

Leave `PAYMENT_LEDGER_APPROVAL_ENABLED` unset until activation is separately
authorized. The new approval path requires PostgreSQL READ COMMITTED and checks
that the Phase 2 database guards are installed and enabled. Receipt submission,
rejection and reconciliation continue through their existing paths.

Verify the actual trigger-to-function bindings, Phase 1 journal and audit
enforcement, and protection of the reviewed receipt revision before activation.
The existing readiness function checks enabled Phase 2 trigger names and tables.
If a receipt submission returns an uncertain commit result, have the student
reload to check the payment status before retrying. Retain the uploaded receipt
for reconciliation and confirm that neither payment nor history references it
before considering cleanup.

## Prerequisites

1. In an isolated disposable PostgreSQL database, verify a fresh upgrade and an
   upgrade from `f78b2c01d4e6` with representative submitted and already verified
   payments. Use the actual Alembic chain. Confirm that the upgrade posts no
   historical allocations and preserves payment and reconciliation history.
2. Verify the migration and Flask logins separately. The migration login owns
   the ledger objects. Provision `learn2play_financial_runtime` as a NOLOGIN
   group before the Phase 1 grants run, and grant membership only to the trusted
   Flask login. If the group was absent during either migration, have the
   migration owner review and install the missing grants before activation.
   The app login must have its existing authorized source-table access,
   including payment and appointment row locking, reviewer and payment-event
   reads, payment updates, and payment-event inserts, sequences and RLS.
   Verify that browser roles cannot use the ledger and that the runtime cannot
   change guards or sealed history. Check effective and inherited grants.
3. Under the intended runtime login, verify a complete approval, an authorized
   replay, rollback after a posting failure, and rejection of verification
   without the matching journal and audit. Validate receipt entry points and
   cancellation and attendance behavior as well.

## Controlled rollout

1. Pause payment approvals and drain all old approval workers and in-flight
   approval transactions. Keep the financial write switch disabled.
2. Apply the reviewed migration as the migration login. Migration
   `c83d9f2a71e4` installs the commit-time guard. Historical verified rows stay
   unallocated. Treat commit of this migration as the database cutover.
3. Start the compatible application with the switch disabled. Confirm the
   migration revision, trigger status, runtime grants, and absence of old
   writers. Check that a missing capability fails closed.
4. After separate authorization, enable the switch for compatible workers and
   resume approvals. A valid previously submitted payment may then be
   verified and allocated. Route incomplete or conflicting evidence to manual
   review. If activation fails, pause approvals again and keep the database
   guard installed while preparing a forward correction.
