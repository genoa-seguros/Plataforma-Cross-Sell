"""Assistente da plataforma (Claude API): um chat, em qualquer tela, que tira dúvidas sobre a plataforma.

Responde com base na documentação (README.md, com as regras de negócio) e nos critérios do Score
(config/criterios.yaml), e consulta os dados da plataforma por ferramentas SÓ DE LEITURA (buscar empresa, ficha,
oportunidades, to-dos, feito na semana). Não altera nada: quando pedem uma mudança, explica onde fazer na tela.
A conversa não é guardada: o navegador manda o histórico a cada pergunta.
Usa o mesmo modelo da temperatura (escolhido na tela Equipe).
"""

import json
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

DOCUMENTOS = (Path("README.md"), Path("config/criterios.yaml"))
MAX_RODADAS = 8  # chamadas à Claude por pergunta (cada consulta de dados é uma rodada)
MAX_RESULTADO = 15000  # caracteres de cada resultado de ferramenta que vão para a IA

SISTEMA = """Você é o assistente da Plataforma Cross-Sell da Innoa, uma corretora de seguros B2B. Quem pergunta é \
alguém da equipe comercial, logado na plataforma. Você ajuda a entender e a usar a plataforma: o que cada tela \
mostra, o que significa cada coluna, filtro, etiqueta e estágio, como o Score e a influência são calculados, por \
que uma empresa está (ou não) em Oportunidades e como fazer cada coisa.

Regras:
- Responda em português do Brasil, curto e direto, em linguagem simples (sem nomes de código, tabelas ou rotas).
- Para regras e telas, use a documentação abaixo. Para dados reais (uma empresa, um Score, o que falta, as \
atividades, o que alguém fez), consulte as ferramentas antes de responder; nunca invente números ou nomes.
- Se a resposta não estiver na documentação nem nos dados, diga que não sabe e sugira falar com o Rodrigo. \
Não invente regras.
- Você só consulta; não altera nada. Se pedirem para mudar algo (criar atividade, estacionar, corrigir a cidade, \
marcar ponte...), explique o passo a passo na tela.
- Ao citar uma empresa que você consultou, use um link no formato [Nome da empresa](#empresa-ID). Para indicar \
uma tela, use [Oportunidades](#oportunidades), [To-dos](#todos), [Negócios em aberto](#negocios), \
[Clientes](#clientes) ou [Diário](#diario).
- Use **negrito** e listas com "- " quando ajudar; nada de tabelas nem títulos."""

FERRAMENTAS = [
    {"name": "buscar_empresas",
     "description": "Procura empresas pelo nome (razão social ou nome fantasia). Devolve id, nome, cidade e se é "
                    "cliente. Use antes das outras ferramentas quando a pessoa citar uma empresa pelo nome.",
     "input_schema": {"type": "object", "properties": {"texto": {"type": "string", "description": "parte do nome"}},
                      "required": ["texto"]}},
    {"name": "ficha_empresa",
     "description": "Dados de uma empresa: cidade, praça, funcionários, setor, pessoas (cargo, área, influência, "
                    "temperatura), negócios do Pipedrive (vigentes, abertos, perdidos), notícias e leitura do site.",
     "input_schema": {"type": "object", "properties": {"empresa_id": {"type": "integer"}}, "required": ["empresa_id"]}},
    {"name": "oportunidades_da_empresa",
     "description": "As oportunidades de uma empresa em cada vertical, analisadas ou não: Score e o porquê, estágio "
                    "do funil, o que falta para entrar, pendências, quem decide, próxima atividade, atualizações, "
                    "produtos sugeridos e se está estacionada.",
     "input_schema": {"type": "object", "properties": {"empresa_id": {"type": "integer"}}, "required": ["empresa_id"]}},
    {"name": "listar_oportunidades",
     "description": "A lista da tela Oportunidades (20 por página, pelo Score), com os mesmos filtros da tela.",
     "input_schema": {"type": "object", "properties": {
         "vertical": {"type": "string", "enum": ["", "saude", "linhas_financeiras", "ramos_elementares"]},
         "oferta": {"type": "string", "enum": ["cross", "mais", "ganhos", "estacionadas"]},
         "ativ": {"type": "string", "enum": ["", "sem", "com"], "description": "sem = nenhuma atividade aberta"},
         "tipo": {"type": "string", "enum": ["", "cliente", "lead"]},
         "pagina": {"type": "integer"}}}},
    {"name": "equipe",
     "description": "As pessoas da equipe (id, nome, papel). Use para achar o id de alguém pelo nome.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "todos",
     "description": "Atividades (To-dos) de uma pessoa da equipe ou de todos, por período.",
     "input_schema": {"type": "object", "properties": {
         "periodo": {"type": "string", "enum": ["semana", "fazer", "vencido", "hoje", "amanha", "proxima"]},
         "usuario_id": {"type": "integer", "description": "omita para a equipe toda"}}}},
    {"name": "feito_na_semana",
     "description": "O que uma pessoa da equipe fez na semana (atividades, atualizações, dados completados à mão).",
     "input_schema": {"type": "object", "properties": {
         "usuario_id": {"type": "integer"},
         "data": {"type": "string", "description": "um dia da semana desejada, AAAA-MM-DD; omita para esta semana"}},
         "required": ["usuario_id"]}},
]


def documentacao() -> str:
    partes = []
    for p in DOCUMENTOS:
        try:
            partes.append(f'<documento nome="{p.name}">\n{p.read_text(encoding="utf-8")}\n</documento>')
        except OSError:
            log.warning("Assistente: %s não encontrado.", p)
    return "\n\n".join(partes)


def _bloco(b) -> dict | None:
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return None


class Assistente:
    def __init__(self, settings, client: anthropic.Anthropic | None = None, modelo: str | None = None):
        self.modelo = modelo or settings.anthropic_model
        self.client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    def responder(self, mensagens: list[dict], executar: Callable[[str, dict], object], *, usuario: str,
                  tela: str = "", hoje: date | None = None) -> str:
        """mensagens: [{"role": "user"|"assistant", "content": texto}], a última da pessoa.
        executar(nome, argumentos) roda uma ferramenta de leitura e devolve algo serializável em JSON."""
        sistema = [
            {"type": "text", "text": SISTEMA + "\n\n" + documentacao(), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": f"Hoje é {(hoje or date.today()):%d/%m/%Y}. Quem pergunta: {usuario}. "
                                     f"Tela aberta agora: {tela or 'não informada'}."},
        ]
        conversa = list(mensagens)
        textos: list[str] = []
        for _ in range(MAX_RODADAS):
            try:
                resp = self.client.beta.messages.create(
                    model=self.modelo, max_tokens=2048, system=sistema, tools=FERRAMENTAS, messages=conversa,
                    output_config={"effort": "medium"},
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            except anthropic.RateLimitError:
                return "Muitas perguntas ao mesmo tempo agora. Tente de novo em um minuto."
            except anthropic.APIStatusError as e:
                log.warning("Claude API recusou o assistente (%s): %s", e.status_code, e.message)
                return "Não consegui falar com a IA agora. Tente de novo em instantes."
            except anthropic.APIConnectionError:
                return "Sem conexão com a IA agora. Tente de novo em instantes."
            if resp.stop_reason == "refusal":
                return "Não posso ajudar com isso."
            blocos = [x for x in (_bloco(b) for b in resp.content) if x]
            textos = [b["text"] for b in blocos if b["type"] == "text"]
            usos = [b for b in blocos if b["type"] == "tool_use"]
            if resp.stop_reason != "tool_use" or not usos:
                break
            conversa.append({"role": "assistant", "content": blocos})
            resultados = []
            for u in usos:
                try:
                    dado = json.dumps(executar(u["name"], u["input"] or {}), ensure_ascii=False, default=str)
                    erro = False
                except Exception as e:  # noqa: BLE001 - o erro vai para a IA, que explica à pessoa
                    log.warning("Assistente: ferramenta %s falhou: %s", u["name"], e)
                    dado, erro = f"Erro: {e}", True
                if len(dado) > MAX_RESULTADO:
                    dado = dado[:MAX_RESULTADO] + " …(cortado)"
                resultados.append({"type": "tool_result", "tool_use_id": u["id"], "content": dado, "is_error": erro})
            conversa.append({"role": "user", "content": resultados})
        texto = "\n\n".join(t.strip() for t in textos if t.strip())
        return texto or "Não consegui chegar a uma resposta. Tente perguntar de outro jeito."
