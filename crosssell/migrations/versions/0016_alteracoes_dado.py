"""o que a plataforma preencheu, campo a campo (Diário da análise)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-23 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0016'
down_revision: Union[str, Sequence[str], None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('alteracoes_dado',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('em', sa.DateTime(), nullable=False),
                    sa.Column('fonte', sa.String(), nullable=False),
                    sa.Column('empresa_id', sa.Integer(), nullable=True),
                    sa.Column('pessoa_id', sa.Integer(), nullable=True),
                    sa.Column('campo', sa.String(), nullable=False),
                    sa.Column('antes', sa.String(), nullable=True),
                    sa.Column('depois', sa.String(), nullable=True),
                    sa.ForeignKeyConstraint(['empresa_id'], ['empresas.id'], ),
                    sa.ForeignKeyConstraint(['pessoa_id'], ['pessoas.id'], ),
                    sa.PrimaryKeyConstraint('id'))
    with op.batch_alter_table('alteracoes_dado', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alteracoes_dado_em'), ['em'], unique=False)
        batch_op.create_index(batch_op.f('ix_alteracoes_dado_empresa_id'), ['empresa_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('alteracoes_dado', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alteracoes_dado_empresa_id'))
        batch_op.drop_index(batch_op.f('ix_alteracoes_dado_em'))
    op.drop_table('alteracoes_dado')
