"""tier de Saúde do cliente (aba Clientes)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-21 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('empresas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tier_saude', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('tier_saude_por_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('tier_saude_em', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key('fk_empresas_tier_saude_por_id', 'usuarios', ['tier_saude_por_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('empresas', schema=None) as batch_op:
        batch_op.drop_constraint('fk_empresas_tier_saude_por_id', type_='foreignkey')
        batch_op.drop_column('tier_saude_em')
        batch_op.drop_column('tier_saude_por_id')
        batch_op.drop_column('tier_saude')
