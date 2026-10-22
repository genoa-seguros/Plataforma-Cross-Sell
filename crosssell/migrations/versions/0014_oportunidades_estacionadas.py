"""oportunidades estacionadas (fora da tela Oportunidades por ora)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-22 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('oportunidades_estacionadas',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('empresa_id', sa.Integer(), nullable=False),
                    sa.Column('vertical', sa.String(), nullable=False),
                    sa.Column('usuario_id', sa.Integer(), nullable=False),
                    sa.Column('criada_em', sa.DateTime(), nullable=False),
                    sa.Column('motivo', sa.String(), nullable=False),
                    sa.Column('texto', sa.String(), nullable=True),
                    sa.Column('rever_em', sa.Date(), nullable=True),
                    sa.ForeignKeyConstraint(['empresa_id'], ['empresas.id'], ),
                    sa.ForeignKeyConstraint(['usuario_id'], ['usuarios.id'], ),
                    sa.PrimaryKeyConstraint('id'),
                    sa.UniqueConstraint('empresa_id', 'vertical'))
    with op.batch_alter_table('oportunidades_estacionadas', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_oportunidades_estacionadas_empresa_id'), ['empresa_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('oportunidades_estacionadas', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_oportunidades_estacionadas_empresa_id'))
    op.drop_table('oportunidades_estacionadas')
