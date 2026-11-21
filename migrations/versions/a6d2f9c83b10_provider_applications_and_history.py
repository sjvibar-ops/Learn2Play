
"""add provider application history and credential ownership

Adapted from updated c4a7d91e5b26, using a fresh revision identity. Additive:

* role_applications.submitted_at       nullable DateTime  (latest submission time)
* credentials.application_id           nullable Integer FK -> role_applications.id
* role_application_history             new append-only table

Existing rows are left untouched: every existing credentials row keeps
application_id = NULL ("application unknown") and is never assigned to a
mentor or coach application by this migration.

Revision ID: a6d2f9c83b10
Revises: f4c2a8d91e70
Create Date: 2026-11-15 00:00:00.000000

Downgrade is refused to preserve application history and credential ownership.

This revision has NOT been executed against any database by its author.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a6d2f9c83b10'
down_revision = 'f4c2a8d91e70'
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
    sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id', name='fk_role_application_history_actor_id', ondelete='RESTRICT'), nullable=True),
    sa.Column('decision_note', sa.String(length=500), nullable=True),
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


    dialect = op.get_bind().dialect.name
    if dialect == 'sqlite':
        for operation in ('UPDATE', 'DELETE'):
            op.execute(f"CREATE TRIGGER role_application_history_no_{operation.lower()} BEFORE {operation} ON role_application_history BEGIN SELECT RAISE(ABORT, 'Provider application history is append-only'); END")
    if dialect == 'postgresql':
        op.execute("CREATE FUNCTION prevent_role_application_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Provider application history is append-only'; END; $$")
        op.execute('CREATE TRIGGER role_application_history_no_mutation BEFORE UPDATE OR DELETE ON role_application_history FOR EACH ROW EXECUTE FUNCTION prevent_role_application_history_mutation()')
        op.execute('CREATE TRIGGER role_application_history_no_truncate BEFORE TRUNCATE ON role_application_history FOR EACH STATEMENT EXECUTE FUNCTION prevent_role_application_history_mutation()')
        op.execute('ALTER TABLE public.role_application_history ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL ON TABLE public.role_application_history FROM PUBLIC')
        op.execute('REVOKE ALL ON SEQUENCE public.role_application_history_id_seq FROM PUBLIC')
        for role in ('anon', 'authenticated'):
            op.execute(f"""DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE ALL ON TABLE public.role_application_history FROM {role};
                    REVOKE ALL ON SEQUENCE public.role_application_history_id_seq FROM {role};
                END IF;
            END $$""")


def downgrade():
    raise RuntimeError('Provider application history cannot be removed through downgrade. Review a forward migration and retention policy.')
