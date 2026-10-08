"""Leitura do site da empresa pela IA (Claude API), para Linhas Financeiras.

Baixa a página inicial e até 3 páginas internas que costumam dizer o que interessa (sobre, clientes,
investidores, serviços), tira o texto e pergunta à IA quatro coisas:
  fundos e investidores   a empresa é um fundo/gestora ou tem fundos e investidores (VC, PE) no capital
  grandes clientes        atende empresas grandes ou conhecidas (logos, cases)
  serviço intelectual     vende conhecimento (consultoria, tecnologia, saúde, jurídico...): E&O
  site profissional       site cuidado, com conteúdo institucional, não uma página improvisada
O texto do site não é guardado; fica só o resultado (Empresa.site_ia) e a data da leitura (site_ia_em).
Usa o mesmo modelo da temperatura (escolhido na tela Equipe).
"""

import json
import logging
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import anthropic
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import Empresa

log = logging.getLogger(__name__)

VALIDADE_DIAS = 180  # o site é relido depois disso
MAX_TEXTO = 15000  # caracteres de texto do site enviados à IA (página inicial + internas)
MAX_PAGINAS_INTERNAS = 3
_INTERNAS = re.compile(r"sobre|about|quem-somos|quem_somos|empresa|clientes|clients|cases|investidores|investors|"
                       r"ri\b|servicos|services|solucoes|solutions|portfolio", re.I)

SISTEMA = """Você analisa o site de uma empresa brasileira para uma corretora de seguros que vende Linhas Financeiras \
(D&O, E&O, Cyber e IMI, o seguro de fundos e gestoras).

Você recebe o texto de algumas páginas do site. Responda só com base no que está escrito:
- fundo_gestora: a própria empresa é um fundo de investimento, gestora de recursos, asset, private equity ou venture \
capital.
- fundos_investidores: a empresa diz ter fundos, investidores ou aporte de capital (venture capital, private equity, \
rodada de investimento, empresa investida, capital aberto na bolsa). Cite os nomes em "investidores" quando houver.
- grandes_clientes: o site mostra clientes grandes ou conhecidos (logos, cases, depoimentos de empresas). Cite até 5 \
nomes em "clientes".
- servico_intelectual: a empresa vende conhecimento ou serviço profissional (consultoria, tecnologia/software, \
saúde, jurídico, contabilidade, engenharia, arquitetura, auditoria, corretagem, educação).
- site_profissional: o site é cuidado e institucional (quem somos, serviços, contato, conteúdo próprio), não uma \
página improvisada, em construção ou só com um formulário.
- servico: em poucas palavras, o que a empresa vende.
- resumo: uma frase em português com o que mais pesa para Linhas Financeiras, sem dados pessoais.
Na dúvida, responda false."""

FORMATO = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "fundo_gestora": {"type": "boolean"},
            "fundos_investidores": {"type": "boolean"},
            "investidores": {"type": "array", "items": {"type": "string"}},
            "grandes_clientes": {"type": "boolean"},
            "clientes": {"type": "array", "items": {"type": "string"}},
            "servico_intelectual": {"type": "boolean"},
            "site_profissional": {"type": "boolean"},
            "servico": {"type": "string"},
            "resumo": {"type": "string"},
        },
        "required": ["fundo_gestora", "fundos_investidores", "investidores", "grandes_clientes", "clientes",
                     "servico_intelectual", "site_profissional", "servico", "resumo"],
        "additionalProperties": False,
    },
}


class _Texto(HTMLParser):
    """Texto visível e links de uma página (sem scripts, estilos e menus de código)."""

    def __init__(self):
        super().__init__()
        self.partes: list[str] = []
        self.links: list[str] = []
        self.titulo = ""
        self._ignorar = 0
        self._no_titulo = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "template"):
            self._ignorar += 1
        elif tag == "title":
            self._no_titulo = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag == "meta":
            a = dict(attrs)
            if (a.get("name") or a.get("property") or "").lower() in ("description", "og:description") and a.get("content"):
                self.partes.append(a["content"])

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template") and self._ignorar:
            self._ignorar -= 1
        elif tag == "title":
            self._no_titulo = False

    def handle_data(self, data):
        if self._ignorar:
            return
        if self._no_titulo:
            self.titulo += data
        texto = " ".join(data.split())
        if texto:
            self.partes.append(texto)


def texto_da_pagina(html: str) -> tuple[str, list[str]]:
    p = _Texto()
    try:
        p.feed(html or "")
    except Exception:  # HTML quebrado: fica o que deu para ler
        pass
    return " ".join(p.partes), p.links


def endereco(e: Empresa) -> str | None:
    if e.website:
        return e.website if e.website.startswith("http") else f"https://{e.website}"
    return f"https://{e.dominio}" if e.dominio else None


def baixar_site(url: str, http: httpx.Client) -> tuple[str, list[str]]:
    """Texto da página inicial e das internas mais úteis, e as páginas lidas."""
    r = http.get(url)
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
        return "", []
    base = str(r.url)
    texto, links = texto_da_pagina(r.text)
    paginas, blocos = [base], [f"<pagina url=\"{base}\">\n{texto[:MAX_TEXTO // 2]}\n</pagina>"]
    host = urlparse(base).netloc.removeprefix("www.")
    internas = []
    for href in links:
        u = urljoin(base, href).split("#")[0]
        if urlparse(u).netloc.removeprefix("www.") == host and u not in paginas and u not in internas \
                and _INTERNAS.search(urlparse(u).path or ""):
            internas.append(u)
    for u in internas[:MAX_PAGINAS_INTERNAS]:
        try:
            ri = http.get(u)
        except httpx.HTTPError:
            continue
        if ri.status_code == 200:
            t, _ = texto_da_pagina(ri.text)
            paginas.append(u)
            blocos.append(f"<pagina url=\"{u}\">\n{t[:MAX_TEXTO // 4]}\n</pagina>")
    return "\n\n".join(blocos)[:MAX_TEXTO], paginas


class LeitorSite:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None, modelo: str | None = None):
        self.settings = settings
        self.modelo = modelo or settings.anthropic_model
        self.client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    def analisar(self, empresa: str, texto: str) -> dict | None:
        if not texto.strip():
            return None
        try:
            resp = self.client.beta.messages.create(
                model=self.modelo,
                max_tokens=1024,
                system=SISTEMA,
                messages=[{"role": "user", "content": f"Empresa: {empresa}\n\n{texto}"}],
                output_config={"effort": "low", "format": FORMATO},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError:
            log.warning("Limite de requisições da Claude API; o site fica para a próxima rodada.")
            return None
        except anthropic.APIStatusError as e:
            log.warning("Claude API recusou a leitura do site (%s): %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.warning("Sem conexão com a Claude API.")
            return None
        if resp.stop_reason == "refusal":
            return None
        bloco = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            dados = json.loads(bloco)
        except json.JSONDecodeError:
            return None
        return dados if isinstance(dados, dict) and "servico_intelectual" in dados else None


def ler_empresa(e: Empresa, leitor: LeitorSite, http: httpx.Client) -> dict | None:
    """Lê o site e devolve o resultado (com as páginas lidas), ou {"erro": ...} quando não deu para ler o site.
    None: a IA não respondeu (tenta de novo na próxima rodada)."""
    url = endereco(e)
    if not url:
        return {"erro": "sem site"}
    try:
        texto, paginas = baixar_site(url, http)
    except httpx.HTTPError:
        return {"erro": "site fora do ar", "site": url}
    if len(texto) < 200:
        return {"erro": "site sem texto (página em construção ou só imagem)", "site": url, "paginas": paginas}
    res = leitor.analisar(e.nome_fantasia or e.razao_social, texto)
    if res is None:
        return None
    res["investidores"], res["clientes"] = res.get("investidores", [])[:5], res.get("clientes", [])[:5]
    res["resumo"], res["servico"] = (res.get("resumo") or "")[:300], (res.get("servico") or "")[:120]
    return {**res, "site": url, "paginas": paginas, "modelo": leitor.modelo}


def precisa(e: Empresa, agora: datetime) -> bool:
    return bool(endereco(e)) and (e.site_ia_em is None or e.site_ia_em < agora - timedelta(days=VALIDADE_DIAS))


def atualizar(db: Session, empresa_ids: list[int], leitor: LeitorSite, http: httpx.Client | None = None,
              limite: int = 20) -> dict:
    """Lê o site das empresas (na ordem recebida, a do Score) que ainda não foram lidas ou passaram da validade."""
    http = http or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (CrossSell)"})
    agora = datetime.utcnow()
    por_id = {e.id: e for e in db.scalars(select(Empresa).where(Empresa.id.in_(empresa_ids)))}
    cont = {"lidos": 0, "sem_site": 0, "falhas_ia": 0}
    for eid in empresa_ids:
        e = por_id.get(eid)
        if e is None or not precisa(e, agora):
            continue
        if cont["lidos"] + cont["sem_site"] + cont["falhas_ia"] >= limite:
            break
        res = ler_empresa(e, leitor, http)
        if res is None:
            cont["falhas_ia"] += 1
            continue
        e.site_ia, e.site_ia_em = res, agora
        cont["sem_site" if "erro" in res else "lidos"] += 1
        db.commit()
    return cont
