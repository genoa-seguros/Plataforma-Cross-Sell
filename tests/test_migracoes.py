"""Migrações (Alembic): o banco que elas criam é o que crosssell/models.py descreve."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

import crosssell.models  # noqa: F401
from crosssell.db import ESQUEMA_INICIAL, Base, init_db


def _banco():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _versao(eng) -> str:
    with eng.connect() as con:
        return con.scalar(text("SELECT version_num FROM alembic_version"))


def test_migracoes_batem_com_os_modelos():
    # Se falhar: mudou crosssell/models.py sem migração. Rode `alembic revision --autogenerate -m "..."`.
    eng = _banco()
    init_db(eng)
    with eng.connect() as con:
        diferencas = compare_metadata(MigrationContext.configure(con, opts={"compare_type": True}), Base.metadata)
    assert diferencas == []


def test_banco_criado_antes_das_migracoes_e_marcado_sem_perder_dados():
    eng = _banco()
    Base.metadata.create_all(eng)  # como os bancos eram criados antes
    with eng.begin() as con:
        con.execute(text("INSERT INTO sync_log (fonte, inicio, registros) VALUES ('pipedrive', '2026-10-01', 7)"))
    init_db(eng)
    assert _versao(eng) == ESQUEMA_INICIAL
    with eng.connect() as con:
        assert con.scalar(text("SELECT registros FROM sync_log")) == 7


def test_init_db_pode_rodar_sempre():
    eng = _banco()
    init_db(eng)
    tabelas = set(inspect(eng).get_table_names())
    init_db(eng)  # sem migração pendente: não faz nada
    assert set(inspect(eng).get_table_names()) == tabelas and "alembic_version" in tabelas
