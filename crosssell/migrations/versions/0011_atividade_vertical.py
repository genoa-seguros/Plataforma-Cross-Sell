"""oportunidade (vertical) da atividade

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20 17:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('atividades', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vertical', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('atividades', schema=None) as batch_op:
        batch_op.drop_column('vertical')
