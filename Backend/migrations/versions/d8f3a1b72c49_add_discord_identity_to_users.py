"""add discord identity to users

Batch 3.1B-1: persistent Discord account linking.

Revision ID: d8f3a1b72c49
Revises: c4a7d91e5b26
Create Date: 2026-11-16

Adds three NULLABLE columns to ``users`` plus a UNIQUE index on
``discord_user_id``:

  * discord_user_id    VARCHAR(32)  - Discord snowflake, always a string
  * discord_username   VARCHAR(100)
  * discord_linked_at  DATETIME     - naive UTC

Existing rows are unaffected (all three stay NULL). The unique index allows
many NULLs and at most one row per Discord ID.

Dependency: this revision must apply on top of c4a7d91e5b26 (the verified head
at the time of writing: 0720c7d1e4ca -> 9e45f7e7aa32 -> c4a7d91e5b26 -> this).

DEPLOYMENT WARNING: the User model now selects these columns on EVERY user
query (including login). An existing database that has not been upgraded to
this revision will fail with "no such column"/"column does not exist" errors.
Prepare the schema explicitly BEFORE running code from this batch against any
existing database. Databases created fresh via db.create_all() (e.g. tests)
already include the columns.

Downgrade drops only the index and the three columns created here; any linked
Discord identities are lost. Export them first if they matter.

This revision has NOT been executed against any database by its author.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8f3a1b72c49'
down_revision = 'c4a7d91e5b26'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('discord_user_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('discord_username', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('discord_linked_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_discord_user_id'), ['discord_user_id'], unique=True)


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_discord_user_id'))
        batch_op.drop_column('discord_linked_at')
        batch_op.drop_column('discord_username')
        batch_op.drop_column('discord_user_id')
