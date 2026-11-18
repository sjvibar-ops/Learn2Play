"""add discord channel fields to appointments

Batch 3.2A-2A: persistent Discord private voice-channel tracking on
appointments. Schema only; no provisioning logic exists yet.

Revision ID: e5a1b9c3d4f8
Revises: d8f3a1b72c49
Create Date: 2026-11-16

Adds to ``appointments``:

  * discord_channel_id               VARCHAR(32)  NULL  - Discord snowflake,
                                                          always a string
  * discord_channel_created_at       DATETIME     NULL  - naive UTC
  * discord_channel_status           VARCHAR(20)  NOT NULL
                                     server default 'unprovisioned'
  * discord_channel_last_attempt_at  DATETIME     NULL  - naive UTC

plus a UNIQUE index ``ix_appointments_discord_channel_id`` on
``discord_channel_id`` (many NULLs allowed, at most one row per channel ID).

Valid discord_channel_status values: unprovisioned, provisioning, provisioned,
failed, uncertain, deprovisioned. No CHECK constraint is added.

Existing rows are preserved: the ID and both timestamps stay NULL and the
status reads 'unprovisioned' through the server default.

Dependency: applies on top of d8f3a1b72c49 (chain: 0720c7d1e4ca ->
9e45f7e7aa32 -> c4a7d91e5b26 -> d8f3a1b72c49 -> this).

DEPLOYMENT WARNING: the Appointment model selects these columns on EVERY
appointment query. An existing database that has not been upgraded to this
revision will fail with "no such column"/"column does not exist" errors.
Prepare the schema explicitly BEFORE running code from this batch against any
existing database. Databases created fresh via db.create_all() (e.g. tests)
already include the columns.

Downgrade drops only the index and the four columns created here; any stored
Discord channel IDs, timestamps and statuses are lost. Export them first if
they matter.

This revision has NOT been executed against any database by its author.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5a1b9c3d4f8'
down_revision = 'd8f3a1b72c49'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('discord_channel_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('discord_channel_created_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(
            'discord_channel_status',
            sa.String(length=20),
            server_default='unprovisioned',
            nullable=False,
        ))
        batch_op.add_column(sa.Column('discord_channel_last_attempt_at', sa.DateTime(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_appointments_discord_channel_id'),
            ['discord_channel_id'],
            unique=True,
        )


def downgrade():
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_appointments_discord_channel_id'))
        batch_op.drop_column('discord_channel_last_attempt_at')
        batch_op.drop_column('discord_channel_status')
        batch_op.drop_column('discord_channel_created_at')
        batch_op.drop_column('discord_channel_id')
