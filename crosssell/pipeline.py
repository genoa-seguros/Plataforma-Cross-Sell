"""Orquestra a atualização: fontes -> enriquecimento -> scores."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import SyncLog, Usuario
from crosssell.scoring import relacionamento


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
    """Cria os usuários da equipe inicial do config que ainda não existem (sem senha: precisam de convite)."""
    n = 0
    for u in settings.usuarios_iniciais():
        reg = db.scalar(select(Usuario).where(Usuario.email == u["email"]))
        if reg is None:
            db.add(Usuario(email=u["email"], nome=u["nome"], verticais=u["verticais"], lider=u["lider"]))
            n += 1
    db.commit()
    return n


def recalcular(db: Session, settings: Settings | None = None) -> dict:
    return {"relacionamento": relacionamento.calcular(db)}
