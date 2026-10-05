from sqlalchemy import create_engine
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
