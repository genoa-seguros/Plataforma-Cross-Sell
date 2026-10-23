"""funil de Oportunidades: validade da atualização e oportunidades que viraram negócio

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-24 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0017'
down_revision: Union[str, Sequence[str], None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('notas_oportunidade', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vale_ate', sa.Date(), nullable=True))
    op.create_table('oportunidades_convertidas',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('empresa_id', sa.Integer(), nullable=False),
                    sa.Column('vertical', sa.String(), nullable=False),
                    sa.Column('produto', sa.String(), nullable=True),
                    sa.Column('pipedrive_deal_id', sa.Integer(), nullable=False),
                    sa.Column('usuario_id', sa.Integer(), nullable=False),
                    sa.Column('criada_em', sa.DateTime(), nullable=False),
                    sa.ForeignKeyConstraint(['empresa_id'], ['empresas.id'], ),
                    sa.ForeignKeyConstraint(['usuario_id'], ['usuarios.id'], ),
                    sa.PrimaryKeyConstraint('id'))
    with op.batch_alter_table('oportunidades_convertidas', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_oportunidades_convertidas_criada_em'), ['criada_em'], unique=False)
        batch_op.create_index(batch_op.f('ix_oportunidades_convertidas_empresa_id'), ['empresa_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_oportunidades_convertidas_pipedrive_deal_id'), ['pipedrive_deal_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('oportunidades_convertidas', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_oportunidades_convertidas_pipedrive_deal_id'))
        batch_op.drop_index(batch_op.f('ix_oportunidades_convertidas_empresa_id'))
        batch_op.drop_index(batch_op.f('ix_oportunidades_convertidas_criada_em'))
    op.drop_table('oportunidades_convertidas')
    with op.batch_alter_table('notas_oportunidade', schema=None) as batch_op:
        batch_op.drop_column('vale_ate')
