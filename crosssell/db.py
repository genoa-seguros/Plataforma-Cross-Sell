import zlib
from contextlib import contextmanager

from sqlalchemy import create_engine, text
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


def init_db(bind=None) -> None:
    from crosssell import models  # noqa: F401  (registra as tabelas)

    Base.metadata.create_all(bind or engine)


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
