"""Assinatura dos e-mails lida pela IA (Claude API): nome completo, cargo e telefone de quem escreve à equipe.

Na leitura das caixas, quem escreveu algo novo e ainda não tem cargo tem os últimos e-mails (só o trecho novo
de cada resposta) lidos pela IA, que tira da assinatura o nome completo, o cargo e o telefone. Só preenche o que
está vazio (nunca sobrescreve o que veio do LinkedIn, do Pipedrive ou de quem preencheu à mão) e lê cada pessoa
uma vez (Pessoa.assinatura_em). Com o cargo, a pessoa ganha área: a ponte passa a ter cargo e, se for da área
da vertical, vira quem decide. O texto dos e-mails não é guardado.
Usa o mesmo modelo da temperatura (escolhido na tela Equipe).
"""

import json
import logging
import re
from datetime import datetime

import anthropic
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import Pessoa
from crosssell.normalize import classificar_senioridade, normalizar_nome_pessoa

log = logging.getLogger(__name__)

MAX_EMAILS = 3  # os mais recentes; a assinatura costuma se repetir
MAX_CARACTERES = 1500  # do fim de cada e-mail, onde fica a assinatura
LIMITE_RODADA = 40  # pessoas por leitura das caixas

SISTEMA = """Você lê a assinatura de e-mails que um contato de uma empresa cliente escreveu para uma corretora de \
seguros. Você recebe o trecho final de alguns e-mails dessa pessoa e o endereço de e-mail dela.

Responda só com base na ASSINATURA de quem escreveu (o bloco com nome, cargo, empresa e telefone no fim):
- nome: o nome completo como está na assinatura. Vazio se não houver assinatura.
- cargo: o cargo como está escrito (ex.: "Gerente de Recursos Humanos", "CFO", "Analista Financeiro"). Vazio se a \
assinatura não trouxer cargo. Não deduza o cargo pelo assunto do e-mail.
- telefone: o telefone da assinatura, como está escrito. Vazio se não houver.
Ignore assinaturas de outras pessoas citadas no e-mail, avisos legais e respostas automáticas. Na dúvida, vazio."""

FORMATO = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"nome": {"type": "string"}, "cargo": {"type": "string"}, "telefone": {"type": "string"}},
        "required": ["nome", "cargo", "telefone"],
        "additionalProperties": False,
    },
}


class LeitorAssinatura:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None, modelo: str | None = None):
        self.settings = settings
        self.modelo = modelo or settings.anthropic_model
        self.client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    def ler(self, email: str, emails: list[dict]) -> dict | None:
        """emails: [{"data": datetime, "texto": str}]. None se a IA não respondeu (tenta de novo depois)."""
        corpo = "\n\n".join(f"<email>\n{e['texto'].strip()[-MAX_CARACTERES:]}\n</email>"
                            for e in emails if (e.get("texto") or "").strip())
        if not corpo:
            return {"nome": "", "cargo": "", "telefone": ""}
        try:
            resp = self.client.beta.messages.create(
                model=self.modelo,
                max_tokens=256,
                system=SISTEMA,
                messages=[{"role": "user", "content": f"E-mail do contato: {email}\n\n{corpo}"}],
                output_config={"effort": "low", "format": FORMATO},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError:
            log.warning("Limite de requisições da Claude API; a assinatura fica para a próxima leitura.")
            return None
        except anthropic.APIStatusError as e:
            log.warning("Claude API recusou a leitura da assinatura (%s): %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.warning("Sem conexão com a Claude API.")
            return None
        if resp.stop_reason == "refusal":
            return {"nome": "", "cargo": "", "telefone": ""}
        texto = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            dados = json.loads(texto)
        except json.JSONDecodeError:
            return None
        if not isinstance(dados, dict):
            return None
        limpo = lambda k, n: " ".join(str(dados.get(k) or "").split()).strip(" -|,;")[:n]  # noqa: E731
        return {"nome": limpo("nome", 80), "cargo": limpo("cargo", 120), "telefone": limpo("telefone", 40)}


def _nome_provisorio(p: Pessoa) -> bool:
    """Nome que veio do endereço de e-mail ("joao.silva") ou só o primeiro nome: a assinatura pode completar."""
    local = (p.email or "").split("@")[0].lower()
    return (bool(local) and p.nome.strip().lower() == local) or len(p.nome.split()) < 2 or bool(re.search(r"[._@\d]", p.nome))


def aplicar(p: Pessoa, res: dict, agora: datetime) -> bool:
    """Preenche o que está vazio; True se gravou algum dado."""
    p.assinatura_em = agora
    mudou = False
    if res.get("cargo") and not p.cargo:
        p.cargo = res["cargo"]
        p.senioridade = p.senioridade or classificar_senioridade(p.cargo)
        mudou = True
    if res.get("telefone") and not p.telefone and re.search(r"\d{4}", res["telefone"]):
        p.telefone, mudou = res["telefone"], True
    if len(res.get("nome", "").split()) >= 2 and _nome_provisorio(p):
        p.nome, p.nome_normalizado, mudou = res["nome"], normalizar_nome_pessoa(res["nome"]), True
    return mudou


def atualizar(db: Session, leitor: LeitorAssinatura, textos_por_pessoa: dict[int, list[dict]],
              agora: datetime | None = None, limite: int = LIMITE_RODADA) -> int:
    """Lê a assinatura de quem escreveu algo novo, ainda não tem cargo e nunca foi lido. Devolve quantos ganharam dado."""
    agora = agora or datetime.utcnow()
    n = feitas = 0
    for pessoa_id, emails in textos_por_pessoa.items():
        if feitas >= limite:
            break
        p = db.get(Pessoa, pessoa_id)
        if p is None or p.cargo or p.assinatura_em is not None:
            continue
        res = leitor.ler(p.email or "", sorted(emails, key=lambda e: e["data"])[-MAX_EMAILS:])
        feitas += 1
        if res is None:
            continue
        n += aplicar(p, res, agora)
    db.commit()
    return n


def historico(db: Session, settings: Settings, leitor: LeitorAssinatura, empresa_ids: set[int], client=None,
              dias: int = 365) -> dict:
    """Leitura única dos últimos 12 meses: para as pessoas sem cargo (e nunca lidas) das empresas pedidas (as com
    oportunidade), relê as caixas, junta os e-mails que cada uma escreveu e lê a assinatura. Quem não escreveu
    nesse período fica como está (é lido quando escrever)."""
    from sqlalchemy import or_, select

    from crosssell.connectors.email_m365 import GraphClient, textos_de

    pessoas = db.scalars(select(Pessoa).where(Pessoa.empresa_id.in_(empresa_ids), Pessoa.email.is_not(None),
                                              or_(Pessoa.cargo.is_(None), Pessoa.cargo == ""),
                                              Pessoa.assinatura_em.is_(None))).all()
    if not pessoas:
        return {"pessoas": 0, "com_emails": 0, "preenchidas": 0}
    textos, falhas = textos_de(db, client or GraphClient(settings), {p.email: p.id for p in pessoas}, dias)
    preenchidas = atualizar(db, leitor, textos, limite=len(textos))
    if falhas:
        raise RuntimeError(f"{len(falhas)} caixas não foram lidas ({preenchidas} pessoas preenchidas): " + "; ".join(falhas))
    return {"pessoas": len(pessoas), "com_emails": len(textos), "preenchidas": preenchidas}
