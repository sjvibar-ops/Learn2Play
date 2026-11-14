
"""enforce unique appointment meeting rooms

Revision ID: 9e45f7e7aa32
Revises: 0720c7d1e4ca
Create Date: 2026-11-06 23:21:48.674241

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9e45f7e7aa32'
down_revision = '0720c7d1e4ca'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('appointments') as batch_op:
        batch_op.create_unique_constraint('uq_appointments_meeting_room', ['meeting_room'])


def downgrade():
    with op.batch_alter_table('appointments') as batch_op:
        batch_op.drop_constraint('uq_appointments_meeting_room', type_='unique')
