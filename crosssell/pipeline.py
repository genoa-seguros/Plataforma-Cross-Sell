"""Orquestra a atualização completa: fontes -> enriquecimento -> scores -> oportunidades."""

from datetime import datetime

from sqlalchemy.orm import Session

from crosssell.models import SyncLog
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


def recalcular(db: Session) -> dict:
    return {
        "relacionamento": relacionamento.calcular(db),
        "oportunidades": oportunidades.calcular(db),
    }
