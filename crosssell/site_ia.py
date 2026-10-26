"""Leitura do site da empresa pela IA (Claude API), para todas as verticais.

Baixa a página inicial e até 4 páginas internas que costumam dizer o que interessa (sobre, equipe, clientes,
investidores, serviços, contato), tira o texto e pergunta à IA:
  fundos e investidores   a empresa é um fundo/gestora ou tem fundos e investidores (VC, PE) no capital (LF)
  grandes clientes        atende empresas grandes ou conhecidas (logos, cases) (LF)
  serviço intelectual     vende conhecimento (consultoria, tecnologia, saúde, jurídico...): E&O (LF)
  site profissional       site cuidado, com conteúdo institucional, não uma página improvisada (LF)
  setor                   o setor da empresa: vale como setor (fonte "site") quando não há o do LinkedIn nem à mão
  cidade da sede          o endereço da sede, quando a empresa ainda não tem cidade (praça de Saúde)
  pessoas                 nomes e cargos da página de equipe/diretoria: candidatos a quem decide (fonte "site")
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
MAX_PAGINAS_INTERNAS = 4
VERSAO = 2  # leituras de antes (só LF, sem setor, sede e pessoas) são refeitas uma vez
MAX_PESSOAS = 8
# Site montado por JavaScript chega sem texto e sem links: tenta direto os endereços institucionais mais comuns
CAMINHOS_COMUNS = ("/sobre", "/quem-somos", "/sobre-nos", "/a-empresa", "/empresa", "/about", "/about-us")
_INTERNAS = re.compile(r"sobre|about|quem-somos|quem_somos|empresa|clientes|clients|cases|investidores|investors|"
                       r"ri\b|servicos|services|solucoes|solutions|portfolio|equipe|time\b|team|lideranca|diretoria|"
                       r"socios|contato|contact|fale-conosco", re.I)

SISTEMA = """Você analisa o site de uma empresa brasileira para uma corretora de seguros que vende Linhas Financeiras \
(D&O, E&O, Cyber e IMI, o seguro de fundos e gestoras), Saúde e Ramos Elementares.

Você recebe o texto de algumas páginas do site. Responda só com base no que está escrito:
- fundo_gestora: a atividade PRINCIPAL da empresa é ser fundo de investimento, gestora de recursos, asset, private \
equity ou venture capital. Se a gestão de recursos for só uma área ou unidade de outro negócio (ex.: banco, \
instituição de pagamento ou fintech que também tem uma gestora), responda false e marque fundos_investidores.
- fundos_investidores: a empresa diz ter fundos, investidores ou aporte de capital (venture capital, private equity, \
rodada de investimento, empresa investida, capital aberto na bolsa), ou tem uma área de gestão de fundos. Cite os nomes \
em "investidores" quando houver.
- grandes_clientes: o site mostra clientes grandes ou conhecidos (logos, cases, depoimentos de empresas). Cite até 5 \
nomes em "clientes".
- servico_intelectual: a empresa vende conhecimento ou serviço profissional (consultoria, tecnologia/software, \
saúde, jurídico, contabilidade, engenharia, arquitetura, auditoria, corretagem, educação).
- site_profissional: o site é cuidado e institucional (quem somos, serviços, contato, conteúdo próprio), não uma \
página improvisada, em construção ou só com um formulário.
- servico: em poucas palavras, o que a empresa vende.
- resumo: uma frase em português com o que mais pesa para Linhas Financeiras, sem dados pessoais.
- setor: o setor da empresa em 1 a 4 palavras, no estilo do LinkedIn (ex.: "Tecnologia da informação", "Fintech", "Indústria farmacêutica", "Logística", "Varejo"). Vazio se o site não deixar claro.
- cidade_sede e uf_sede: a cidade e a UF (sigla) do endereço da sede ou matriz escrito no site. Vazio se não houver endereço ou se houver só filiais sem dizer qual é a sede.
- pessoas: sócios, diretores e gestores que o site apresenta pelo nome com o cargo (página de equipe, diretoria, sobre). Nome completo como está escrito e o cargo. Não invente cargos; deixe de fora quem aparece sem cargo, depoimentos de clientes e autores de posts.
Na dúvida, responda false (ou vazio)."""

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
            "setor": {"type": "string"},
            "cidade_sede": {"type": "string"},
            "uf_sede": {"type": "string"},
            "pessoas": {"type": "array", "items": {
                "type": "object", "properties": {"nome": {"type": "string"}, "cargo": {"type": "string"}},
                "required": ["nome", "cargo"], "additionalProperties": False}},
        },
        "required": ["fundo_gestora", "fundos_investidores", "investidores", "grandes_clientes", "clientes",
                     "servico_intelectual", "site_profissional", "servico", "resumo", "setor", "cidade_sede",
                     "uf_sede", "pessoas"],
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


# Navegador comum (alguns sites recusam quem não parece navegador), identificando a plataforma
CABECALHOS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36 CrossSell", "Accept-Language": "pt-BR,pt;q=0.9"}


class SiteIndisponivel(Exception):
    """O site não pôde ser lido; a mensagem diz o porquê (aparece na ficha e no site-teste)."""


def _por_que(exc: httpx.HTTPError) -> str:
    texto = str(exc).lower()
    if isinstance(exc, httpx.TimeoutException):
        return "site não respondeu (tempo esgotado)"
    if isinstance(exc, httpx.ConnectError) and ("name" in texto or "resolve" in texto or "nodename" in texto):
        return "endereço não existe (domínio não encontrado)"
    if isinstance(exc, httpx.ConnectError) and ("ssl" in texto or "certificate" in texto):
        return "site com certificado de segurança inválido"
    return "site fora do ar (não aceitou a conexão)"


def baixar_site(url: str, http: httpx.Client) -> tuple[str, list[str]]:
    """Texto da página inicial e das internas mais úteis, e as páginas lidas. SiteIndisponivel com o motivo
    quando o site recusa a leitura, não existe ou não é uma página."""
    try:
        r = http.get(url)
    except httpx.HTTPError as exc:
        raise SiteIndisponivel(_por_que(exc)) from exc
    if r.status_code in (401, 403, 429):
        raise SiteIndisponivel(f"site recusou a leitura ({r.status_code}: bloqueia robôs)")
    if r.status_code == 404:
        raise SiteIndisponivel("página inicial não encontrada (404)")
    if r.status_code != 200:
        raise SiteIndisponivel(f"site respondeu com erro ({r.status_code})")
    if "html" not in r.headers.get("content-type", "html"):
        raise SiteIndisponivel(f"o endereço não é uma página ({r.headers.get('content-type')})")
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
    if len(texto) < 200 and not internas:
        raiz = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
        internas = [raiz + c for c in CAMINHOS_COMUNS]
    lidas = 0
    for u in internas:
        if lidas >= MAX_PAGINAS_INTERNAS:
            break
        try:
            ri = http.get(u)
        except httpx.HTTPError:
            continue
        t = texto_da_pagina(ri.text)[0] if ri.status_code == 200 else ""
        if len(t) >= 40:  # página vazia (erro ou só script) não conta
            paginas.append(str(ri.url))
            blocos.append(f"<pagina url=\"{ri.url}\">\n{t[:MAX_TEXTO // 4]}\n</pagina>")
            lidas += 1
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


MIN_DESCRICAO = 80  # "Sobre" do LinkedIn curto demais não serve de plano B


def ler_empresa(e: Empresa, leitor: LeitorSite, http: httpx.Client) -> dict | None:
    """Lê o site e devolve o resultado (com as páginas lidas). Se o site não puder ser lido, a IA lê o "Sobre" da
    empresa no LinkedIn (plano B; o motivo fica em "problema_site"); sem isso, {"erro": motivo}.
    None: a IA não respondeu (tenta de novo na próxima rodada)."""
    url = endereco(e)
    if not url:
        return {"erro": "sem site"}
    problema, paginas, texto = None, [], ""
    try:
        texto, paginas = baixar_site(url, http)
        if len(texto) < 200:
            problema = "site só com JavaScript ou imagens (sem texto para ler)"
    except SiteIndisponivel as exc:
        problema = str(exc)
    fonte = "site"
    if problema:
        if len((e.descricao or "").strip()) < MIN_DESCRICAO:
            return {"erro": problema, "site": url, "paginas": paginas}
        texto, paginas, fonte = f"<pagina url=\"LinkedIn (Sobre)\">\n{e.descricao.strip()[:MAX_TEXTO]}\n</pagina>", [], "LinkedIn"
    res = leitor.analisar(e.nome_fantasia or e.razao_social, texto)
    if res is None:
        return None
    res["investidores"], res["clientes"] = res.get("investidores", [])[:5], res.get("clientes", [])[:5]
    limpo = lambda t: (t or "").strip().rstrip("}").strip()  # noqa: E731  (às vezes a IA deixa um "}" no fim)
    res["resumo"], res["servico"] = limpo(res.get("resumo"))[:300], limpo(res.get("servico"))[:120]
    res["setor"], res["cidade_sede"] = limpo(res.get("setor"))[:60], limpo(res.get("cidade_sede"))[:60]
    uf = limpo(res.get("uf_sede")).upper()
    res["uf_sede"] = uf if re.fullmatch(r"[A-Z]{2}", uf) else ""
    res["pessoas"] = [{"nome": limpo(x.get("nome"))[:80], "cargo": limpo(x.get("cargo"))[:120]}
                      for x in (res.get("pessoas") or []) if isinstance(x, dict)
                      and len(limpo(x.get("nome")).split()) >= 2 and limpo(x.get("cargo"))][:MAX_PESSOAS]
    res["versao"] = VERSAO
    extra = {"fonte": fonte, "problema_site": problema} if problema else {"fonte": fonte}
    return {**res, **extra, "site": url, "paginas": paginas, "modelo": leitor.modelo}


def precisa(e: Empresa, agora: datetime) -> bool:
    if not endereco(e):
        return False
    if e.site_ia_em is None or e.site_ia_em < agora - timedelta(days=VALIDADE_DIAS):
        return True
    r = e.site_ia or {}
    return "erro" not in r and r.get("versao", 1) < VERSAO  # lida antes de trazer setor, sede e pessoas


def aplicar(db: Session, e: Empresa, res: dict) -> None:
    """Grava o que serve às outras verticais: o setor (se não há o do LinkedIn nem o informado à mão), a cidade da
    sede (se a empresa ainda não tem cidade) e as pessoas com cargo (candidatas a quem decide)."""
    from crosssell.resolver import resolver_pessoa

    if "erro" in res:
        return
    if res.get("setor") and e.setor_fonte not in ("linkedin", "manual"):
        e.setor, e.setor_fonte = res["setor"], "site"
    if res.get("cidade_sede") and not e.cidade:
        e.cidade, e.cidade_fonte = res["cidade_sede"], "site"
        e.uf = e.uf or res.get("uf_sede") or None
    for x in res.get("pessoas") or []:
        resolver_pessoa(db, nome=x["nome"], cargo=x["cargo"], empresa=e, fonte="site")


def atualizar(db: Session, empresa_ids: list[int], leitor: LeitorSite, http: httpx.Client | None = None,
              limite: int = 20) -> dict:
    """Lê o site das empresas (na ordem recebida) que ainda não foram lidas, passaram da validade ou foram lidas
    por uma versão antiga da leitura."""
    http = http or httpx.Client(timeout=15, follow_redirects=True, headers=CABECALHOS)
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
        aplicar(db, e, res)
        cont["sem_site" if "erro" in res else "lidos"] += 1
        db.commit()
    return cont
