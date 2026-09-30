from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from crosssell.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or get_settings().database_url
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    return create_engine(url, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind=None) -> None:
    from crosssell import models  # noqa: F401  (registra as tabelas)

    Base.metadata.create_all(bind or engine)
