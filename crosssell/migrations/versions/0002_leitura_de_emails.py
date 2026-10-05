"""leitura de e-mails por usuário: chave na tela Equipe (desligada por padrão) e resultado da última leitura

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05 18:41:21.055822

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('usuarios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('le_emails', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('leitura_em', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('leitura_erro', sa.String(), nullable=True))



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('usuarios', schema=None) as batch_op:
        batch_op.drop_column('leitura_erro')
        batch_op.drop_column('leitura_em')
        batch_op.drop_column('le_emails')

