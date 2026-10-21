"""registro do que a pessoa fez à mão (log da semana em To-dos)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('registros',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('usuario_id', sa.Integer(), nullable=False),
                    sa.Column('em', sa.DateTime(), nullable=False),
                    sa.Column('tipo', sa.String(), nullable=False),
                    sa.Column('empresa_id', sa.Integer(), nullable=True),
                    sa.Column('texto', sa.String(), nullable=False),
                    sa.ForeignKeyConstraint(['empresa_id'], ['empresas.id'], ),
                    sa.ForeignKeyConstraint(['usuario_id'], ['usuarios.id'], ),
                    sa.PrimaryKeyConstraint('id'))
    with op.batch_alter_table('registros', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_registros_em'), ['em'], unique=False)
        batch_op.create_index(batch_op.f('ix_registros_empresa_id'), ['empresa_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_registros_usuario_id'), ['usuario_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('registros', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_registros_usuario_id'))
        batch_op.drop_index(batch_op.f('ix_registros_empresa_id'))
        batch_op.drop_index(batch_op.f('ix_registros_em'))
    op.drop_table('registros')
