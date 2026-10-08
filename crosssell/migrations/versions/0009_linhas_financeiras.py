"""linhas financeiras: site lido pela IA, portal da noticia, data do negocio perdido

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08 23:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('empresas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('site_ia', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('site_ia_em', sa.DateTime(), nullable=True))
    with op.batch_alter_table('negocios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('perdido_em', sa.Date(), nullable=True))
    with op.batch_alter_table('noticias', schema=None) as batch_op:
        batch_op.add_column(sa.Column('site', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('noticias', schema=None) as batch_op:
        batch_op.drop_column('site')
    with op.batch_alter_table('negocios', schema=None) as batch_op:
        batch_op.drop_column('perdido_em')
    with op.batch_alter_table('empresas', schema=None) as batch_op:
        batch_op.drop_column('site_ia_em')
        batch_op.drop_column('site_ia')
