"""oportunidade incluida a mao, ponte a mao e notas da equipe

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-13 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('pessoas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ponte_manual', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('ponte_manual_por', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('ponte_manual_em', sa.DateTime(), nullable=True))
    op.create_table('oportunidades_manuais',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('empresa_id', sa.Integer(), nullable=False),
                    sa.Column('vertical', sa.String(), nullable=False),
                    sa.Column('usuario_id', sa.Integer(), nullable=False),
                    sa.Column('criada_em', sa.DateTime(), nullable=False),
                    sa.ForeignKeyConstraint(['empresa_id'], ['empresas.id'], ),
                    sa.ForeignKeyConstraint(['usuario_id'], ['usuarios.id'], ),
                    sa.PrimaryKeyConstraint('id'),
                    sa.UniqueConstraint('empresa_id', 'vertical'))
    with op.batch_alter_table('oportunidades_manuais', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_oportunidades_manuais_empresa_id'), ['empresa_id'], unique=False)
    op.create_table('notas_oportunidade',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('empresa_id', sa.Integer(), nullable=False),
                    sa.Column('vertical', sa.String(), nullable=False),
                    sa.Column('texto', sa.String(), nullable=False),
                    sa.Column('usuario_id', sa.Integer(), nullable=False),
                    sa.Column('criada_em', sa.DateTime(), nullable=False),
                    sa.Column('ajuste', sa.Integer(), nullable=True),
                    sa.Column('motivo', sa.String(), nullable=True),
                    sa.Column('avaliada_em', sa.DateTime(), nullable=True),
                    sa.ForeignKeyConstraint(['empresa_id'], ['empresas.id'], ),
                    sa.ForeignKeyConstraint(['usuario_id'], ['usuarios.id'], ),
                    sa.PrimaryKeyConstraint('id'))
    with op.batch_alter_table('notas_oportunidade', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notas_oportunidade_empresa_id'), ['empresa_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('notas_oportunidade', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notas_oportunidade_empresa_id'))
    op.drop_table('notas_oportunidade')
    with op.batch_alter_table('oportunidades_manuais', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_oportunidades_manuais_empresa_id'))
    op.drop_table('oportunidades_manuais')
    with op.batch_alter_table('pessoas', schema=None) as batch_op:
        batch_op.drop_column('ponte_manual_em')
        batch_op.drop_column('ponte_manual_por')
        batch_op.drop_column('ponte_manual')
