"""cota do linkedin por vertical

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07 21:40:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('linkedin_pedidos', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vertical', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('grupo', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('linkedin_pedidos', schema=None) as batch_op:
        batch_op.drop_column('grupo')
        batch_op.drop_column('vertical')
