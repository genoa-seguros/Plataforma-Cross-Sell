"""Orquestra a atualização: fontes -> enriquecimento -> scores."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import SyncLog, Usuario
from crosssell.scoring import relacionamento


# Parâmetros de cada rodada de `crosssell rotina` (também mostrados na aba Rotina)
ROTINA_DIAS = 2  # Pipedrive e e-mails: o que mudou nos últimos N dias (a rodada anterior pode ter falhado)
NOTICIAS_HORAS = 24  # cada empresa no máximo uma vez a cada N horas
RECEITA_LOTE = 50  # empresas com CNPJ ainda não consultadas, por rodada
SITES_LOTE = 50  # sites de empresas sem LinkedIn, por rodada
QUALIDADE_DIAS = 7  # revisão do cadastro: semanal


def registrar(db: Session, fonte: str, fn, *args, **kwargs) -> dict:
    from crosssell import historico  # noqa: F401 (liga o registro do que a plataforma preencheu)

    log = SyncLog(fonte=fonte)
    db.add(log)
    db.commit()
    db.info["origem"] = fonte  # o que este passo preencher vai para o Diário da análise
    try:
        res = fn(*args, **kwargs)
        log.registros = sum(v for v in res.values() if isinstance(v, int))
        return res
    except Exception as exc:  # registra e propaga
        db.rollback()
        log.erro = repr(exc)[:2000]
        raise
    finally:
        db.info.pop("origem", None)
        log.fim = datetime.utcnow()
        db.merge(log)
        db.commit()


def carregar_usuarios(db: Session, settings: Settings) -> int:
    """Cria os usuários da lista do config que ainda não existem (sem senha). Só para a demonstração
    (scripts/demo.py) e os testes: em produção, quem cria usuários é o master, pela tela Equipe."""
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
