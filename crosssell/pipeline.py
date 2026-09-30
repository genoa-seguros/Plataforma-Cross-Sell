"""Orquestra a atualização completa: fontes -> enriquecimento -> scores -> oportunidades."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings, get_settings
from crosssell.models import SyncLog, UsuarioInterno
from crosssell.scoring import oportunidades, relacionamento


def registrar(db: Session, fonte: str, fn, *args, **kwargs) -> dict:
    log = SyncLog(fonte=fonte)
    db.add(log)
    db.commit()
    try:
        res = fn(*args, **kwargs)
        log.registros = sum(v for v in res.values() if isinstance(v, int))
        return res
    except Exception as exc:  # registra e propaga
        db.rollback()
        log.erro = repr(exc)[:2000]
        raise
    finally:
        log.fim = datetime.utcnow()
        db.merge(log)
        db.commit()


def carregar_usuarios(db: Session, settings: Settings) -> int:
    """Sincroniza a tabela de usuários internos com config/verticais.yaml."""
    for u in settings.usuarios():
        reg = db.scalar(select(UsuarioInterno).where(UsuarioInterno.email == u["email"]))
        if reg is None:
            reg = UsuarioInterno(email=u["email"])
            db.add(reg)
        reg.nome, reg.verticais, reg.lider = u["nome"], u["verticais"], u["lider"]
    db.commit()
    return len(settings.usuarios())


def recalcular(db: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    carregar_usuarios(db, settings)
    return {
        "relacionamento": relacionamento.calcular(db),
        "oportunidades": oportunidades.calcular(db, settings=settings),
    }
