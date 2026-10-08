"""outros dominios de e-mail da empresa

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08 13:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('empresas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dominios_extras', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('dominios_em', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('empresas', schema=None) as batch_op:
        batch_op.drop_column('dominios_em')
        batch_op.drop_column('dominios_extras')
