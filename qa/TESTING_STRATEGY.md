# LEARN2PLAY test strategy — initial CI foundation

**Unit:** Existing `unittest` modules exercise validation, domain state transitions, REST response mapping, and fencing, with mocked Discord calls.

**Integration:** Existing Flask test-client + disposable SQLite tests cover authorization, CSRF, booking transactions, migrations, and recovery ledger persistence. The CI job executes these together. A dummy PostgreSQL URL only verifies isolation, **not** real PostgreSQL transactional concurrency.

**E2E:** Preserve Antigravity's real Playwright Chromium checks for student/mentor/admin workflows. These must run against a disposable app and mocked Discord endpoints. Browser E2E is **not yet wired to CI** because this ZIP lacks the repo's browser runner/fixtures and we must not invent credentials or fake server startup.

**Performance:** Add a separately approved, local-only load-test harness after identifying the staging runner. Measure booking endpoint p50/p95/p99, provision contention, error rate, recovery claim latency, and database pool exhaustion. Never load-test live Supabase or Discord. Set pass/fail targets after establishing an isolated baseline.

**CI/CD:** `.github/workflows/learn2play-ci.yml` contains a **CI-only** backend and JS syntax job. It contains no deploy stage, no migration execution, and no Discord or Supabase secrets. Inspect actual `Backend/requirements.txt` and repo layout before enabling this workflow; it has not been run on GitHub Actions.

**Feature 3 Part 2:** Manual review endpoints support listing, detail, flagged investigations and audited resolution. They deliberately do **not** perform Discord DELETE, reclaim uncertain creation, or enable automatic cleanup. A future deletion protocol needs explicit, verifiable channel ownership and fencing across workers.
