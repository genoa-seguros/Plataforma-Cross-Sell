from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crosssell.config import Settings
from crosssell.db import Base
import crosssell.models  # noqa: F401

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine():
    from sqlalchemy import create_engine

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


@pytest.fixture
def settings():
    return Settings(_env_file=None, verticais_file=RAIZ / "config" / "verticais.yaml",
                    internal_domains="innoaseguros.com.br", pipedrive_api_token="x")
