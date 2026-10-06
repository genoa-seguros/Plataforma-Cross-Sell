"""Temperatura do relacionamento a partir da escrita do contato (Claude API).

Lê os últimos e-mails que a PESSOA escreveu para a equipe (só a parte nova de
cada resposta, sem o histórico citado) e classifica a abertura dela em
pouca | media | muita, com uma justificativa curta. O texto dos e-mails não é
armazenado; fica só o rótulo e a justificativa.
"""

import json
import logging
from datetime import datetime

import anthropic
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import Configuracao, Pessoa

log = logging.getLogger(__name__)

NIVEIS = ("pouca", "media", "muita")
ROTULO = {"pouca": "Pouca abertura", "media": "Média abertura", "muita": "Muita abertura"}

# Modelos que o master pode escolher na tela Equipe. Todos aceitam o que classificar() usa: effort,
# resposta em JSON estruturado e o fallback do servidor ("default"). O Haiku 4.5 não aceita effort nem o fallback.
MODELOS = {
    "claude-sonnet-5-5": "Claude Sonnet 5.5: US$ 2 / 10 por milhão de tokens (entrada / saída)",
    "claude-opus-5-5": "Claude Opus 5.5: mais capaz, US$ 4 / 20 por milhão de tokens",
}
CHAVE_MODELO = "ia_modelo"


def modelo_em_uso(db: Session, settings: Settings) -> tuple[str, str]:
    """(modelo, origem): o escolhido na tela Equipe; senão ANTHROPIC_MODEL do .env; senão o padrão do código."""
    c = db.get(Configuracao, CHAVE_MODELO)
    if c is not None and c.valor in MODELOS:
        return c.valor, "plataforma"
    return settings.anthropic_model, "servidor"

SISTEMA = """Você avalia a abertura de um contato de uma corretora de seguros B2B com base em como ele escreve.

Você recebe os e-mails mais recentes que o contato enviou para a equipe da corretora (só o texto que ele escreveu).
Classifique a abertura do contato para avançar a conversa comercial:

- muita: responde com disposição, detalha, traz informações ou documentos por iniciativa própria, faz perguntas, \
propõe próximos passos ou reuniões, tom cordial e próximo.
- media: responde de forma educada e objetiva, mas sem iniciativa; atende ao que foi pedido e para por aí.
- pouca: respostas curtas ou evasivas, adia, diz que não é o momento, delega para outra pessoa sem engajar, \
tom frio ou formal demais, ou pede para não ser contatado.

Considere o conjunto dos e-mails, dando mais peso aos mais recentes. Ignore assinaturas, avisos legais e \
respostas automáticas (fora do escritório). Se só houver respostas automáticas, use "media" e diga isso na justificativa.
A justificativa deve ter uma frase, em português, citando o sinal observado, sem copiar dados pessoais."""

FORMATO = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "temperatura": {"type": "string", "enum": list(NIVEIS)},
            "justificativa": {"type": "string"},
        },
        "required": ["temperatura", "justificativa"],
        "additionalProperties": False,
    },
}


class Classificador:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None, modelo: str | None = None):
        self.settings = settings
        self.modelo = modelo or settings.anthropic_model
        # A chave do .env só chega ao SDK por aqui; vazia, o SDK procura nas variáveis de ambiente.
        self.client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    def classificar(self, emails: list[dict]) -> dict | None:
        """emails: [{"data": datetime, "texto": str}], do mais antigo para o mais recente."""
        if not emails:
            return None
        corpo = "\n\n".join(
            f"<email data=\"{e['data']:%d/%m/%Y}\">\n{e['texto'].strip()}\n</email>" for e in emails if e["texto"].strip()
        )
        if not corpo:
            return None
        try:
            resp = self.client.beta.messages.create(
                model=self.modelo,
                max_tokens=1024,
                system=SISTEMA,
                messages=[{"role": "user", "content": corpo}],
                output_config={"effort": "low", "format": FORMATO},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError:
            log.warning("Limite de requisições da Claude API; temperatura fica para a próxima sincronização.")
            return None
        except anthropic.APIStatusError as e:
            log.warning("Claude API recusou a classificação (%s): %s", e.status_code, e.message)
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
        return dados if dados.get("temperatura") in NIVEIS else None


def atualizar(db: Session, classificador: Classificador, textos_por_pessoa: dict[int, list[dict]]) -> int:
    """Classifica e grava a temperatura de cada pessoa que escreveu algo novo."""
    n = 0
    limite = classificador.settings.temperatura_max_emails
    for pessoa_id, emails in textos_por_pessoa.items():
        recentes = sorted(emails, key=lambda e: e["data"])[-limite:]
        res = classificador.classificar(recentes)
        if res is None:
            continue
        p = db.get(Pessoa, pessoa_id)
        p.temperatura = res["temperatura"]
        p.temperatura_motivo = res["justificativa"][:300]
        p.temperatura_em = datetime.utcnow()
        n += 1
    db.commit()
    return n
