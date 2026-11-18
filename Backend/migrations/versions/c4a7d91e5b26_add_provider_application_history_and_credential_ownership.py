
"""add provider application history and credential ownership

Batch 2.1B. Purely additive:

* role_applications.submitted_at       nullable DateTime  (latest submission time)
* credentials.application_id           nullable Integer FK -> role_applications.id
* role_application_history             new append-only table

Existing rows are left untouched: every existing credentials row keeps
application_id = NULL ("application unknown") and is never assigned to a
mentor or coach application by this migration.

Revision ID: c4a7d91e5b26
Revises: 9e45f7e7aa32
Create Date: 2026-11-15 00:00:00.000000

DOWNGRADE WARNING. downgrade() permanently discards data created after the
upgrade: the whole role_application_history table (every recorded resubmission
and every preserved review decision), credentials.application_id (which files
belong to which application; files fall back to being shared across a user's
applications) and role_applications.submitted_at. Export the history table
before downgrading a database that has been used.

This revision has NOT been executed against any database by its author.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4a7d91e5b26'
down_revision = '9e45f7e7aa32'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('role_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('submitted_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('credentials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('application_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_credentials_application_id',
            'role_applications',
            ['application_id'],
            ['id'],
        )
        batch_op.create_index(
            batch_op.f('ix_credentials_application_id'),
            ['application_id'],
            unique=False,
        )

    op.create_table('role_application_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('event_type', sa.String(length=20), nullable=False),
    sa.Column('from_status', sa.String(length=20), nullable=False),
    sa.Column('to_status', sa.String(length=20), nullable=False),
    sa.Column('prior_bio', sa.Text(), nullable=True),
    sa.Column('prior_links', sa.Text(), nullable=True),
    sa.Column('prior_review_note', sa.String(length=500), nullable=True),
    sa.Column('prior_reviewed_by', sa.Integer(), nullable=True),
    sa.Column('prior_reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('prior_submitted_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['role_applications.id'], name='fk_role_application_history_application_id'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_role_application_history_user_id'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('role_application_history', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_role_application_history_application_id'), ['application_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_role_application_history_user_id'), ['user_id'], unique=False)


def downgrade():
    # Destroys the append-only history and the credential-to-application links.
    with op.batch_alter_table('role_application_history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_role_application_history_user_id'))
        batch_op.drop_index(batch_op.f('ix_role_application_history_application_id'))

    op.drop_table('role_application_history')

    with op.batch_alter_table('credentials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_credentials_application_id'))
        batch_op.drop_constraint('fk_credentials_application_id', type_='foreignkey')
        batch_op.drop_column('application_id')

    with op.batch_alter_table('role_applications', schema=None) as batch_op:
        batch_op.drop_column('submitted_at')
