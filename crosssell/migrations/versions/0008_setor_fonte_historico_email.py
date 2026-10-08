"""origem do setor e carga do historico de e-mails

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08 21:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('empresas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('setor_fonte', sa.String(), nullable=True))
    with op.batch_alter_table('usuarios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('historico_em', sa.DateTime(), nullable=True))
    # Setor de empresa já lida no LinkedIn veio de lá (a leitura sobrescreve o setor do Pipedrive)
    op.execute("UPDATE empresas SET setor_fonte = 'linkedin' WHERE setor IS NOT NULL AND linkedin_em IS NOT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('usuarios', schema=None) as batch_op:
        batch_op.drop_column('historico_em')
    with op.batch_alter_table('empresas', schema=None) as batch_op:
        batch_op.drop_column('setor_fonte')
