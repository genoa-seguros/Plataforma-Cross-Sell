"""foto diária da análise (Diário da análise)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-23 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('analise_dia',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('data', sa.Date(), nullable=False),
                    sa.Column('vertical', sa.String(), nullable=False),
                    sa.Column('analisadas', sa.Integer(), nullable=False),
                    sa.Column('fila', sa.Integer(), nullable=False),
                    sa.Column('ids', sa.JSON(), nullable=False),
                    sa.Column('faltando', sa.JSON(), nullable=False),
                    sa.Column('atualizado_em', sa.DateTime(), nullable=False),
                    sa.PrimaryKeyConstraint('id'),
                    sa.UniqueConstraint('data', 'vertical'))
    with op.batch_alter_table('analise_dia', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_analise_dia_data'), ['data'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('analise_dia', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_analise_dia_data'))
    op.drop_table('analise_dia')
