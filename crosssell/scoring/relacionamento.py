"""Score de relacionamento (0–100) por pessoa e por empresa, a partir dos metadados de e-mail.

Pessoa:
  frequência    35%  nº de interações nos últimos 90 dias (escala log, satura em 30)
  recência      30%  decaimento exponencial desde a última interação (meia-vida ~31 dias)
  reciprocidade 25%  fração das threads com ida E volta (conversa real, não disparo)
  amplitude     10%  quantos usuários internos falam com a pessoa (satura em 3)

Empresa:
  melhor contato      40%  score do contato mais forte
  contatos ativos     20%  nº de pessoas com score >= 20 (satura em 5)
  acesso a decisores  20%  existe sócio/C-level/diretor com score >= 20
  presença multi-vertical 20%  nº de verticais em que já é cliente (de 3)
"""

import math
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.models import Empresa, Interacao, Negocio, Pessoa

JANELA_DIAS = 365
DECISORES = {"socio", "c_level", "diretor"}


def score_pessoa(interacoes: list[Interacao], agora: datetime) -> tuple[float, dict]:
    if not interacoes:
        return 0.0, {}
    recentes = [i for i in interacoes if i.data >= agora - timedelta(days=90)]
    freq = min(1.0, math.log1p(len(recentes)) / math.log1p(30))
    dias = (agora - max(i.data for i in interacoes)).days
    rec = math.exp(-max(dias, 0) / 45)
    threads: dict[str, set[str]] = defaultdict(set)
    for i in interacoes:
        threads[i.thread_id or i.message_id].add(i.direcao)
    recip = sum(1 for d in threads.values() if len(d) == 2) / len(threads)
    ampl = min(1.0, len({i.usuario_email for i in interacoes}) / 3)
    comp = {
        "frequencia": round(freq, 3), "recencia": round(rec, 3),
        "reciprocidade": round(recip, 3), "amplitude": round(ampl, 3),
        "interacoes_90d": len(recentes), "dias_ultima": dias,
        "usuarios": sorted({i.usuario_email for i in interacoes}),
    }
    return round(100 * (0.35 * freq + 0.30 * rec + 0.25 * recip + 0.10 * ampl), 1), comp


def verticais_vigentes(negocios: list[Negocio]) -> set[str]:
    return {n.vertical for n in negocios if n.vigente and n.vertical}


def calcular(db: Session, agora: datetime | None = None) -> dict:
    agora = agora or datetime.utcnow()
    inicio = agora - timedelta(days=JANELA_DIAS)

    por_pessoa: dict[int, list[Interacao]] = defaultdict(list)
    for i in db.scalars(select(Interacao).where(Interacao.data >= inicio, Interacao.pessoa_id.is_not(None))):
        por_pessoa[i.pessoa_id].append(i)

    pessoas = db.scalars(select(Pessoa)).all()
    for p in pessoas:
        p.score_relacionamento, p.score_componentes = score_pessoa(por_pessoa.get(p.id, []), agora)
        p.ponto_focal = False

    empresas = db.scalars(select(Empresa)).all()
    for e in empresas:
        contatos = sorted((p for p in e.pessoas), key=lambda p: p.score_relacionamento, reverse=True)
        ativos = [p for p in contatos if p.score_relacionamento >= 20]
        # Pontos focais: até 3 contatos mais fortes com relação mínima.
        for p in ativos[:3]:
            p.ponto_focal = True
        melhor = contatos[0].score_relacionamento / 100 if contatos else 0.0
        decisor = 1.0 if any(p.senioridade in DECISORES for p in ativos) else 0.0
        vert = len(verticais_vigentes(e.negocios)) / 3
        e.score_relacionamento = round(
            100 * (0.40 * melhor + 0.20 * min(1.0, len(ativos) / 5) + 0.20 * decisor + 0.20 * vert), 1
        )
        e.score_componentes = {
            "melhor_contato": round(melhor, 3), "contatos_ativos": len(ativos),
            "acesso_decisor": bool(decisor), "verticais_cliente": sorted(verticais_vigentes(e.negocios)),
            "pontos_focais": [p.id for p in ativos[:3]],
        }
    db.commit()
    return {"pessoas": len(pessoas), "empresas": len(empresas)}
