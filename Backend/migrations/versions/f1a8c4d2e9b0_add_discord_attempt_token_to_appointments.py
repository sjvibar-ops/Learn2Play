"""add discord channel attempt token to appointments

Batch 3.2A-2B-1: one persisted provisioning attempt token on appointments.
Schema only; nothing generates, assigns, claims or checks the token yet.

Revision ID: f1a8c4d2e9b0
Revises: e5a1b9c3d4f8
Create Date: 2026-11-16

Adds to ``appointments``:

  * discord_channel_attempt_token  VARCHAR(64)  NULL  - identifies the one
                                                       provisioning attempt
                                                       currently authorized to
                                                       write the channel
                                                       outcome

NULL means no attempt is authorized. Every existing row stays NULL. No index,
no unique constraint and no server default are added. A later batch will
generate the values with secrets.token_hex(32) (64 hex characters).

Existing rows and all existing Discord channel columns, indexes and unrelated
constraints are left untouched.

Dependency: applies on top of e5a1b9c3d4f8 (chain: 0720c7d1e4ca ->
9e45f7e7aa32 -> c4a7d91e5b26 -> d8f3a1b72c49 -> e5a1b9c3d4f8 -> this).

DEPLOYMENT WARNING: the Appointment model selects this column on EVERY
appointment query. An existing database that has not been upgraded to this
revision will fail with "no such column"/"column does not exist" errors.
Prepare the schema explicitly BEFORE running code from this batch against any
existing database. Databases created fresh via db.create_all() (e.g. tests)
already include the column.

Downgrade drops only this column; any stored attempt tokens are lost.

This revision has NOT been executed against any database by its author.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1a8c4d2e9b0'
down_revision = 'e5a1b9c3d4f8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'discord_channel_attempt_token',
            sa.String(length=64),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.drop_column('discord_channel_attempt_token')
