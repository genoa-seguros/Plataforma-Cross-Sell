"""Migrações do banco (Alembic).

Rodam sozinhas no `crosssell initdb` (e ao subir o servidor), por crosssell.db.init_db. Para criar
uma migração depois de mudar crosssell/models.py, na raiz do projeto:

    alembic revision --autogenerate -m "o que mudou"

Revise o arquivo gerado em crosssell/migrations/versions/ antes do commit.
"""

from logging.config import fileConfig

from alembic import context

import crosssell.models  # noqa: F401  (registra as tabelas)
from crosssell.db import Base, engine

config = context.config
if config.config_file_name:  # só na linha de comando (alembic.ini); no init_db não há arquivo
    fileConfig(config.config_file_name)


def _migrar(conexao) -> None:
    context.configure(
        connection=conexao,
        target_metadata=Base.metadata,
        compare_type=True,
        # O SQLite (desenvolvimento) não altera colunas: o modo batch recria a tabela
        render_as_batch=conexao.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Migrações só rodam conectadas ao banco (sem --sql).")

conexao = config.attributes.get("connection")
if conexao is not None:  # chamado por crosssell.db.init_db, dentro da transação dele
    _migrar(conexao)
else:  # linha de comando do alembic: usa o DATABASE_URL do .env, como o app
    with engine.begin() as conexao:
        _migrar(conexao)
