# Discord integration plan

Source reviewed locally: `origin/updated` at `70bd5e044ac56618b5b922388fead58dd3a6ac52`. The current branch uses JaaS for meetings. Discord is optional and can be added separately once the migration and access policies are agreed.

## What is in the source branch

- **Account linking and guild membership.** `Backend/routes.py` exposes authenticated connect, callback, status, guild-status, and disconnect routes. `Backend/discord_oauth.py` uses OAuth2 with the identity scope and calls Discord's REST API for guild membership. The integration associates a Discord identity with an existing Learn2Play account.
- **Private voice channels.** The appointment Discord route, `Backend/discord_channel_orchestration.py`, and `Backend/discord_provisioning.py` handle optional channel creation and per-participant access. The Flask server uses bot credentials for REST calls. This source does not establish a separate bot process, gateway intents, bot commands, external webhooks, booking notifications, or scheduled reminders. The reported live server and any separately hosted bots require confirmation from the groupmate.
- **Cleanup and operator review.** `Backend/discord_lifecycle.py`, `discord_reconciliation.py`, `discord_cleanup_fencing.py`, `discord_cleanup_safety.py`, `discord_channel_retirement.py`, `discord_deletion_intent.py`, and `discord_deletion_dispatch.py` implement recovery, an operator queue, and guarded deletion. `Backend/routes.py` also exposes admin-only recovery endpoints.
- **UI and tests.** Use the relevant sections of `Frontend/static/js/settings.js`, `Frontend/static/js/sessions.js`, `Frontend/templates/settings.html`, and `Frontend/templates/sessions.html` as references. The corresponding OAuth, guild, provisioning, lifecycle, and reconciliation tests are under `Backend/tests/`.

## Integration order

1. Confirm who owns the Discord application, guild, and bot, and whether any bot is deployed independently. Flask's REST integration can run with the existing web app. A separate gateway bot or scheduled worker would need its own hosting and an explicit interface if the team chooses to add one.
2. Port account linking behind an optional configuration boundary. Start with `Backend/discord_oauth.py` and its authenticated routes. Add a new additive migration to the current Alembic head for the fields equivalent to `d8f3a1b72c49`. Verify unique Discord identity ownership, signed-in user-bound expiring OAuth state, replay handling, disconnect, and that linking grants no Learn2Play role. Mock all external HTTP calls.
3. Add optional guild status, then private channels. Adapt the schema represented by `e5a1b9c3d4f8`, `f1a8c4d2e9b0`, and `a7c8e9d0f1b2`. Require current appointment membership, exact provider approval, eligible appointment state, and the same payment eligibility as JaaS before exposing or creating a room. Preserve the current appointment-lock and payment-lock ordering. Keep remote calls outside locked transactions, and test retries, duplicate creation, timeouts, revoked or disconnected identities, permissions, and Discord outages. Let JaaS continue to work independently.
4. Add recovery review and deletion in a separate phase. Adapt `b8d9e0f1a2c3`, `c9e0f1a2b3d4`, and `d0a1b2c3d4e5`. Attach cleanup to the current cancellation, rejection, and attendance finalization paths. Require admin authorization and CSRF for operator actions, verify Discord channel provenance and guild membership before deletion, and test crossed retries and uncertain REST outcomes on PostgreSQL with fake HTTP.

The branch's Discord migrations descend from its alternate provider-application revision `c4a7d91e5b26`. The current branch already has an adapted provider application schema and merges payments with gamification before the ledger and SQLite index repair revisions. Port only the Discord schema deltas as fresh, reviewed revisions. Review existing account identities, foreign keys, permissions, and the app's migration head on disposable databases before opting in. Preserve current registration, payments, booking, attendance, and JaaS code.

## Configuration

The Flask server needs `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, and `DISCORD_REDIRECT_URI` for OAuth; `DISCORD_GUILD_ID` and `DISCORD_BOT_TOKEN` for guild checks; and the `DISCORD_SESSIONS_ENABLED` switch for channel provisioning. Keep credentials on the server and configure the exact callback URL in the Discord application. Give the bot only the permissions required for membership lookup and approved channel operations. Confirm the role hierarchy and the intended student, provider, and staff visibility in a separately approved test guild.

No Discord code is enabled by this checkpoint. The integration requires new schema, payment and cleanup policy decisions, and controlled external verification. Keep it outside the local onboarding and booking pilot until those dependencies are approved.
