import zlib
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from crosssell.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or get_settings().database_url
    # Provedores entregam "postgres://..."; o SQLAlchemy usa o driver psycopg 3
    for prefixo in ("postgres://", "postgresql://"):
        if url.startswith(prefixo):
            url = "postgresql+psycopg://" + url[len(prefixo):]
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    return create_engine(url, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


MIGRACOES = Path(__file__).parent / "migrations"
ESQUEMA_INICIAL = "0001"  # crosssell/migrations/versions/0001_esquema_inicial.py


def config_alembic(conexao):
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRACOES))
    cfg.attributes["connection"] = conexao
    return cfg


def init_db(bind=None) -> None:
    """Deixa o banco na versão mais nova das migrações (crosssell/migrations). Pode rodar sempre:
    sem migração pendente, não faz nada."""
    from alembic import command

    eng = bind or engine
    with eng.begin() as conexao:
        if eng.dialect.name == "postgresql":  # dois processos subindo juntos: um migra, o outro espera
            conexao.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": zlib.crc32(b"crosssell-migracoes")})
        cfg = config_alembic(conexao)
        tabelas = set(inspect(conexao).get_table_names())
        if "empresas" in tabelas and "alembic_version" not in tabelas:
            # Banco criado pelo create_all, antes das migrações: já tem o esquema inicial
            command.stamp(cfg, ESQUEMA_INICIAL)
        command.upgrade(cfg, "head")


@contextmanager
def trava(nome: str, bind=None):
    """Trava entre processos e servidores (advisory lock do Postgres). Devolve False se outro processo
    já está com ela. O Postgres solta a trava sozinho se o processo morrer. No SQLite (desenvolvimento)
    não trava."""
    eng = bind or engine
    if eng.dialect.name != "postgresql":
        yield True
        return
    chave = zlib.crc32(nome.encode())
    with eng.connect() as con:
        livre = con.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": chave})
        con.commit()  # a trava é da sessão: não precisa (nem deve) ficar com a transação aberta
        try:
            yield livre
        finally:
            if livre:
                con.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": chave})
                con.commit()
