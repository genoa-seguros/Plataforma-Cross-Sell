"""Notas da equipe sobre uma oportunidade, lidas pela IA (Claude API), que ajusta o Score.

A equipe escreve o que aconteceu (ex.: "a CAPIM tem um corretor da família, muito difícil entrar"). A IA lê as
notas da empresa naquela vertical, da mais antiga para a mais recente, e devolve um ajuste de -50 a +20 pontos
com uma frase de motivo. Vale o ajuste da nota mais recente; ele aparece no "Por quê" como nota da equipe.
Usa o mesmo modelo da temperatura (escolhido na tela Equipe).
"""

import json
import logging
from datetime import datetime

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import VERTICAL_LABEL, Settings
from crosssell.models import Empresa, NotaOportunidade

log = logging.getLogger(__name__)

AJUSTE_MIN, AJUSTE_MAX = -50, 20  # uma nota derruba bem; sozinha não leva uma conta ruim ao topo

SISTEMA = """Você ajuda uma corretora de seguros B2B a priorizar oportunidades de venda cruzada.

Você recebe as notas que a equipe escreveu sobre uma empresa numa vertical (Saúde, Linhas Financeiras ou Ramos \
Elementares), da mais antiga para a mais recente. Elas contam o que aconteceu: conversas, obstáculos, sinais de \
interesse. Decida quanto ajustar o Score da oportunidade, de -50 a +20 pontos:
- muito negativo (-30 a -50): entrada quase impossível (corretor da família ou sócio, contrato longo recém-renovado, \
pediu para não ser contatado, empresa fechando);
- negativo (-10 a -30): obstáculo relevante, mas contornável (renovou há pouco, decisor resistente, sem orçamento agora);
- neutro (0): nota só informativa;
- positivo (+5 a +20): sinal concreto de abertura (pediu cotação, insatisfeito com o corretor atual, renovação próxima).
Dê mais peso às notas recentes. O motivo deve ter uma frase curta, em português, sem dados pessoais."""

FORMATO = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"ajuste": {"type": "integer"}, "motivo": {"type": "string"}},
        "required": ["ajuste", "motivo"],
        "additionalProperties": False,
    },
}


class LeitorNotas:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None, modelo: str | None = None):
        self.settings = settings
        self.modelo = modelo or settings.anthropic_model
        self.client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    def avaliar(self, empresa: str, vertical: str, notas: list[NotaOportunidade]) -> dict | None:
        corpo = f"Empresa: {empresa}\nVertical: {VERTICAL_LABEL.get(vertical, vertical)}\n\n" + "\n\n".join(
            f"<nota data=\"{n.criada_em:%d/%m/%Y}\">\n{n.texto.strip()}\n</nota>" for n in notas)
        try:
            resp = self.client.beta.messages.create(
                model=self.modelo,
                max_tokens=512,
                system=SISTEMA,
                messages=[{"role": "user", "content": corpo}],
                output_config={"effort": "low", "format": FORMATO},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError:
            log.warning("Limite de requisições da Claude API; a nota é avaliada na próxima rodada.")
            return None
        except anthropic.APIStatusError as e:
            log.warning("Claude API recusou a avaliação da nota (%s): %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.warning("Sem conexão com a Claude API.")
            return None
        if resp.stop_reason == "refusal":
            return None
        texto = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            dados = json.loads(texto)
        except json.JSONDecodeError:
            return None
        if not isinstance(dados, dict) or not isinstance(dados.get("ajuste"), int):
            return None
        return {"ajuste": max(AJUSTE_MIN, min(AJUSTE_MAX, dados["ajuste"])),
                "motivo": (dados.get("motivo") or "").strip().rstrip("}").strip()[:200]}


def avaliar(db: Session, leitor: LeitorNotas, empresa_id: int, vertical: str) -> NotaOportunidade | None:
    """Avalia as notas da empresa na vertical e grava o ajuste na mais recente."""
    notas = db.scalars(select(NotaOportunidade).where(NotaOportunidade.empresa_id == empresa_id,
                                                      NotaOportunidade.vertical == vertical)
                       .order_by(NotaOportunidade.criada_em, NotaOportunidade.id)).all()
    if not notas:
        return None
    e = db.get(Empresa, empresa_id)
    res = leitor.avaliar(e.nome_fantasia or e.razao_social, vertical, notas[-12:])
    if res is None:
        return None
    ultima = notas[-1]
    ultima.ajuste, ultima.motivo, ultima.avaliada_em = res["ajuste"], res["motivo"], datetime.utcnow()
    db.commit()
    return ultima


def pendentes(db: Session, leitor: LeitorNotas) -> dict:
    """Rotina: avalia as notas que a IA não conseguiu avaliar na hora (ex.: Claude API fora do ar)."""
    cont = {"avaliadas": 0, "falhas": 0}
    vistos = set()
    for n in db.scalars(select(NotaOportunidade).where(NotaOportunidade.avaliada_em.is_(None))).all():
        if (n.empresa_id, n.vertical) in vistos:
            continue
        vistos.add((n.empresa_id, n.vertical))
        cont["avaliadas" if avaliar(db, leitor, n.empresa_id, n.vertical) else "falhas"] += 1
    return cont
