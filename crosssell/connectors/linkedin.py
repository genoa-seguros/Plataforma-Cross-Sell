"""LinkedIn pela Linked API, sem planilha intermediária.

Caminho principal (executar): a plataforma chama a Linked API direto. A cada rodada
(a rotina roda de hora em hora) ela confere os pedidos em andamento, aplica os que
terminaram e inicia até `linkedin_lote` novos, sem passar de `linkedin_limite_dia`
em 24 h. A ordem segue a tabela (maior score primeiro) e depois as Oportunidades.

Tipos de pedido:
  ler     perfil ou página da empresa (com decisores e posts) de quem já tem endereço
  buscar  procurar o perfil/página pelo nome; a plataforma escolhe o candidato certo.
          Pessoa encontrada já fica com título e endereço, sem gastar uma leitura.
  area    funcionários da empresa com cargo da área que decide a vertical (ex.: RH
          para Saúde), quando ainda não conhecemos ninguém dessa área.

Alternativa antiga, via n8n (disparar/receber pelo webhook):
  1. A plataforma levanta os alvos: empresas e contatos dos negócios abertos que
     nunca foram lidos no LinkedIn ou cuja leitura passou da validade. Quem tem
     endereço vai com acao="ler" (fetch); quem não tem vai com acao="buscar"
     (search) e a plataforma escolhe o candidato certo no retorno. Antes disso,
     o link da empresa é procurado no próprio site dela (descobrir_por_site).
  2. A plataforma chama o webhook do n8n (POST), um alvo por chamada, com o
     endereço de retorno. O n8n responde na hora e processa em segundo plano.
  3. O n8n consulta a Linked API (fetchPerson / fetchCompany) para cada alvo.
  4. O n8n devolve cada resultado em POST /api/integracoes/linkedin/resultados,
     com o mesmo token no cabeçalho Authorization. A plataforma atualiza pessoas,
     empresas, decisores e posts.

Um alvo enviado fica "em andamento" por PRAZO_PEDIDO e não é reenviado nesse
período. O perfil só é aceito se o nome bater com o da plataforma (evita gravar
um homônimo quando o n8n precisou buscar o perfil pelo nome).
"""

import json
import re
import secrets
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.config import AREAS_VERTICAL
from crosssell.models import Empresa, LinkedinPedido, Noticia, Pessoa
from crosssell.normalize import (
    classificar_area, classificar_senioridade, dominio_site, nome_para_busca, normalizar_nome_empresa, normalizar_nome_pessoa,
)
from crosssell.resolver import resolver_pessoa

PRAZO_PEDIDO = timedelta(hours=36)
BUSCA_VALIDADE = timedelta(days=30)
FUNCIONARIOS = "funcionarios"  # chave em Empresa.linkedin_areas: última leitura da lista de funcionários  # quem não foi encontrado é procurado de novo depois disso
CAMPOS_RESULTADO = ("id_alvo", "tipo", "linkedin_url", "nome", "headline", "cargo_atual", "empresa_atual",
                    "localizacao", "setor", "funcionarios", "site", "sede", "decisores", "posts", "capturado_em", "erro")


def direto(settings: Settings) -> bool:
    return bool(settings.linked_api_token and settings.linked_api_identification_token)


def via_n8n(settings: Settings) -> bool:
    return bool(settings.n8n_linkedin_webhook_url and settings.n8n_token and settings.plataforma_url)


def configurado(settings: Settings) -> bool:
    return direto(settings) or via_n8n(settings)


def token_valido(settings: Settings, cabecalho: str | None) -> bool:
    esperado = f"Bearer {settings.n8n_token}"
    return bool(settings.n8n_token) and secrets.compare_digest((cabecalho or "").encode(), esperado.encode())


# --- 1. Alvos --------------------------------------------------------------

def _precisa(lido_em: datetime | None, pedido_em: datetime | None, validade_dias: int, agora: datetime) -> bool:
    if pedido_em and pedido_em > agora - PRAZO_PEDIDO and (lido_em is None or lido_em < pedido_em):
        return False  # já foi pedido e ainda não voltou
    return lido_em is None or lido_em < agora - timedelta(days=validade_dias)


def _termos(nome: str | None) -> set[str]:
    return {t for t in normalizar_nome_empresa(nome or "").split() if len(t) >= 3}


def _candidatos_alvo(db: Session, settings: Settings) -> tuple[dict[int, str], dict[int, str], dict[int, set]]:
    """Empresas e pessoas a manter atualizadas, em ordem de prioridade (Oportunidades pelo
    Potencial, depois as empresas dos negócios abertos), e as verticais em jogo em cada empresa."""
    from crosssell import tabela

    empresas: dict[int, str] = {}
    pessoas: dict[int, str] = {}
    verticais: dict[int, set] = {}
    for op in tabela.oportunidades(db, settings):
        eid = op["empresa"]["id"]
        empresas.setdefault(eid, f"oportunidade: {op['verticalNome']}")
        verticais.setdefault(eid, set()).add(op["vertical"])
        for c in op["contatos"]:
            pessoas.setdefault(c["id"], "contato da empresa")
    for linha in tabela.montar(db, settings):
        if linha["empresa"]:
            empresas.setdefault(linha["empresa"]["id"], f"{linha['funil']}: {linha['titulo']}")
            if linha["vertical"]:
                verticais.setdefault(linha["empresa"]["id"], set()).add(linha["vertical"])
        if linha["pessoa"]:
            pessoas.setdefault(linha["pessoa"]["id"], "contato do negócio")
        for c in linha["contatos"]:
            pessoas.setdefault(c["id"], "contato da empresa")
    return empresas, pessoas, verticais


def _acao(obj, validade: int, agora: datetime) -> str | None:
    """'ler' (tem endereço), 'buscar' (procurar o perfil) ou None (nada a fazer agora)."""
    if obj.linkedin_url:
        return "ler" if _precisa(obj.linkedin_em, obj.linkedin_pedido_em, validade, agora) else None
    if obj.linkedin_nao_encontrado and obj.linkedin_busca_em and obj.linkedin_busca_em > agora - BUSCA_VALIDADE:
        return None  # já procurado sem sucesso; aparece em "não encontrados"
    return "buscar" if _precisa(obj.linkedin_busca_em, obj.linkedin_pedido_em, 0, agora) else None


def _alvo_empresa(e: Empresa, acao: str, motivo: str) -> dict:
    return {"id_alvo": f"E{e.id}", "tipo": "empresa", "acao": acao, "nome": e.nome_fantasia or e.razao_social,
            "empresa": e.razao_social, "cargo": "", "email": "", "linkedin_url": e.linkedin_url or "",
            "site": e.website or (f"https://{e.dominio}" if e.dominio else ""), "dominio": e.dominio or "",
            "cnpj": e.cnpj or "", "busca": nome_para_busca(e.nome_fantasia or e.razao_social),
            "motivo": motivo}


def _alvo_pessoa(p: Pessoa, acao: str, motivo: str) -> dict:
    empresa = nome_para_busca(p.empresa.nome_fantasia or p.empresa.razao_social) if p.empresa else ""
    return {"id_alvo": f"P{p.id}", "tipo": "pessoa", "acao": acao, "nome": p.nome, "empresa": empresa,
            "empresa_busca": empresa,
            "cargo": p.cargo or "", "email": p.email or "", "linkedin_url": p.linkedin_url or "", "site": "",
            "dominio": p.empresa.dominio if p.empresa and p.empresa.dominio else "", "cnpj": "",
            "busca": f"{p.nome} {empresa}".strip(), "motivo": motivo}


def _area_de(p: Pessoa) -> str | None:
    return classificar_area(p.cargo) or classificar_area(p.linkedin_headline)


def _alvos_area(e: Empresa, verticais: set, validade: int, agora: datetime, motivo: str) -> list[dict]:
    """Ler os funcionários da empresa quando falta alguém da área que decide alguma vertical em jogo.
    Uma consulta por empresa: o filtro de cargo da Linked API é ignorado (testado com SumUp e Salvy),
    então a lista vem geral e a plataforma classifica cada pessoa pela área do título.
    Só depois da página da empresa lida (os decisores podem já resolver)."""
    if not e.linkedin_url or not e.linkedin_em:
        return []
    conhecidas = {_area_de(p) for p in e.pessoas}
    faltam = [AREAS_VERTICAL[v][0] for v in sorted(verticais)
              if AREAS_VERTICAL.get(v) and not conhecidas & set(AREAS_VERTICAL[v])]
    feita = (e.linkedin_areas or {}).get(FUNCIONARIOS)
    if not faltam or (feita and datetime.fromisoformat(feita) > agora - timedelta(days=validade)):
        return []
    return [{**_alvo_empresa(e, "area", motivo), "area": faltam[0]}]


def alvos(db: Session, settings: Settings, agora: datetime | None = None) -> list[dict]:
    """Alvos pendentes, na ordem de prioridade: a empresa e os contatos de cada negócio juntos."""
    agora = agora or datetime.utcnow()
    validade = settings.linkedin_validade_dias
    empresas, pessoas, verticais = _candidatos_alvo(db, settings)
    ordem_e = {eid: i for i, eid in enumerate(empresas)}
    saida = []
    for e in db.scalars(select(Empresa).where(Empresa.id.in_(empresas))):
        acao = _acao(e, validade, agora)
        if acao:
            saida.append((ordem_e[e.id], 0, _alvo_empresa(e, acao, empresas[e.id])))
        saida += [(ordem_e[e.id], 2, a) for a in _alvos_area(e, verticais.get(e.id, set()), validade, agora,
                                                              empresas[e.id])]
    for p in db.scalars(select(Pessoa).where(Pessoa.id.in_(pessoas))):
        acao = _acao(p, validade, agora)
        if acao:
            saida.append((ordem_e.get(p.empresa_id, len(ordem_e)), 1, _alvo_pessoa(p, acao, pessoas[p.id])))
    return [a for *_, a in sorted(saida, key=lambda x: (x[0], x[1]))]


def situacao(db: Session, settings: Settings) -> dict:
    fila = alvos(db, settings)
    empresas, pessoas, _ = _candidatos_alvo(db, settings)
    nao = [*db.scalars(select(Empresa).where(Empresa.id.in_(empresas), Empresa.linkedin_nao_encontrado.is_(True),
                                            Empresa.linkedin_url.is_(None))),
           *db.scalars(select(Pessoa).where(Pessoa.id.in_(pessoas), Pessoa.linkedin_nao_encontrado.is_(True),
                                           Pessoa.linkedin_url.is_(None)))]
    conta = lambda acao, tipo: sum(a["acao"] == acao and a["tipo"] == tipo for a in fila)  # noqa: E731
    return {
        "ler": {"empresas": conta("ler", "empresa"), "pessoas": conta("ler", "pessoa")},
        "buscar": {"empresas": conta("buscar", "empresa"), "pessoas": conta("buscar", "pessoa")},
        "areas": sum(a["acao"] == "area" for a in fila),
        "direto": direto(settings), "limiteDia": settings.linkedin_limite_dia,
        "ultimas24h": _pedidos_24h(db, datetime.utcnow()),
        "naoEncontrados": [{"id_alvo": f"{'E' if isinstance(o, Empresa) else 'P'}{o.id}",
                            "tipo": "empresa" if isinstance(o, Empresa) else "pessoa",
                            "nome": o.razao_social if isinstance(o, Empresa) else o.nome,
                            "empresa": "" if isinstance(o, Empresa) else (o.empresa.razao_social if o.empresa else "")}
                           for o in nao[:50]],
    }


def definir_endereco(db: Session, id_alvo: str, url: str) -> None:
    """Endereço informado à mão (casos que a busca não resolveu)."""
    url = url.strip()
    if not re.match(r"^https?://([a-z]{2,3}\.)?linkedin\.com/(in|company|school)/", url, re.I):
        raise ValueError("Informe um endereço do LinkedIn, como https://www.linkedin.com/in/nome-sobrenome.")
    obj = db.get(Pessoa if id_alvo[:1] == "P" else Empresa, int(id_alvo[1:])) if id_alvo[1:].isdigit() else None
    if obj is None:
        raise ValueError("Alvo não encontrado.")
    obj.linkedin_url = url
    obj.linkedin_nao_encontrado = False
    db.commit()


# --- 1b. Endereço da empresa pelo próprio site (sem usar a Linked API) ------

LINK_EMPRESA = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[A-Za-z0-9\-_%.]+", re.I)


def link_no_html(html: str) -> str | None:
    m = LINK_EMPRESA.search(html or "")
    return m.group(0).rstrip("/.") if m else None


def descobrir_por_site(db: Session, settings: Settings, http: httpx.Client | None = None, limite: int = 50) -> dict:
    empresas, _, _ = _candidatos_alvo(db, settings)
    http = http or httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (CrossSell)"})
    cont = {"verificadas": 0, "encontradas": 0}
    pendentes = db.scalars(select(Empresa).where(Empresa.id.in_(empresas), Empresa.linkedin_url.is_(None),
                                                Empresa.dominio.is_not(None), Empresa.linkedin_site_em.is_(None))
                           .limit(limite)).all()
    for e in pendentes:
        e.linkedin_site_em = datetime.utcnow()
        cont["verificadas"] += 1
        for url in (f"https://{e.dominio}", f"https://www.{e.dominio}"):
            try:
                r = http.get(url)
            except httpx.HTTPError:
                continue
            link = link_no_html(r.text) if r.status_code == 200 else None
            if link:
                e.linkedin_url = link
                cont["encontradas"] += 1
                break
    db.commit()
    return cont


# --- 2. Disparo do n8n -----------------------------------------------------

def disparar(db: Session, settings: Settings, http: httpx.Client | None = None) -> dict:
    """Envia ao webhook do n8n até `linkedin_lote` alvos, UM POR CHAMADA, e marca-os como pedidos.

    Um alvo por execução porque a Linked API responde de forma assíncrona: o n8n
    fica parado num nó Wait até o resultado daquele alvo chegar.
    """
    if not configurado(settings):
        raise RuntimeError("Integração com o n8n não configurada (N8N_LINKEDIN_WEBHOOK_URL, N8N_TOKEN, PLATAFORMA_URL).")
    lote = alvos(db, settings)[: settings.linkedin_lote]
    if not lote:
        return {"enviados": 0}
    http = http or httpx.Client(timeout=30)
    agora = datetime.utcnow()
    lote_id = f"{agora:%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
    callback = settings.plataforma_url.rstrip("/") + "/api/integracoes/linkedin/resultados"
    enviados, falhas = [], 0
    for a in lote:
        try:
            r = http.post(settings.n8n_linkedin_webhook_url, json={"lote_id": lote_id, "callback_url": callback, "alvos": [a]},
                          headers={"Authorization": f"Bearer {settings.n8n_token}"})
            r.raise_for_status()
        except httpx.HTTPError:
            falhas += 1
            continue
        obj = db.get(Pessoa if a["tipo"] == "pessoa" else Empresa, int(a["id_alvo"][1:]))
        obj.linkedin_pedido_em = agora
        enviados.append(a)
    db.commit()
    if not enviados and falhas:
        raise RuntimeError("O n8n recusou todos os envios (confira a URL do webhook e o token).")
    return {"enviados": len(enviados), "falhas": falhas, "leituras": sum(a["acao"] == "ler" for a in enviados),
            "buscas": sum(a["acao"] == "buscar" for a in enviados), "lote": lote_id}


# --- 4. Resultados ---------------------------------------------------------

def _data(valor) -> datetime:
    v = str(valor or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v[:19].replace("Z", ""), fmt)
        except ValueError:
            continue
    return datetime.utcnow()


def _lista(valor) -> list[dict]:
    """Aceita lista de objetos, JSON em texto ou linhas "Nome | Cargo | URL"."""
    if isinstance(valor, list):
        return [d for d in valor if isinstance(d, dict)]
    v = str(valor or "").strip()
    if not v:
        return []
    try:
        return _lista(json.loads(v))
    except json.JSONDecodeError:
        itens = []
        for linha in v.splitlines():
            partes = [x.strip() for x in linha.split("|")]
            if partes and partes[0]:
                itens.append({"nome": partes[0], "headline": partes[1] if len(partes) > 1 else "",
                              "linkedin_url": partes[2] if len(partes) > 2 else ""})
        return itens


def _num(valor) -> int | None:
    """'1.200', '201-500' ou 350 -> número (no intervalo, o limite superior)."""
    digitos = re.findall(r"\d[\d.]*", str(valor or ""))
    return int(digitos[-1].replace(".", "")) if digitos else None


def _txt(r: dict, campo: str) -> str:
    v = r.get(campo)
    return str(v).strip() if v not in (None, "") else ""


def mesmo_nome(a: str, b: str) -> bool:
    ta, tb = normalizar_nome_pessoa(a).split(), set(normalizar_nome_pessoa(b).split())
    return bool(ta) and ta[0] in tb and ta[-1] in tb


def _aplicar_pessoa(p: Pessoa, r: dict, quando: datetime) -> str:
    if _txt(r, "nome") and not mesmo_nome(p.nome, _txt(r, "nome")):
        return "nao_confirmado"
    p.linkedin_url = _txt(r, "linkedin_url") or p.linkedin_url
    p.linkedin_headline = _txt(r, "headline") or p.linkedin_headline
    p.linkedin_empresa_atual = _txt(r, "empresa_atual") or p.linkedin_empresa_atual
    if _txt(r, "cargo_atual") and not p.cargo:
        p.cargo = _txt(r, "cargo_atual")
    if p.cargo or p.linkedin_headline:
        p.senioridade = classificar_senioridade(p.cargo or p.linkedin_headline)
    p.linkedin_em = quando
    return "pessoas"


FUNCIONARIOS_MANUAL_DIAS = 180


def manual_em_vigor(e: Empresa, agora: datetime) -> bool:
    """O número de funcionários foi informado à mão há menos de FUNCIONARIOS_MANUAL_DIAS."""
    return (e.funcionarios_fonte == "manual" and e.funcionarios_em is not None
            and e.funcionarios_em > agora - timedelta(days=FUNCIONARIOS_MANUAL_DIAS))


def _aplicar_empresa(db: Session, e: Empresa, r: dict, quando: datetime) -> int:
    e.linkedin_url = _txt(r, "linkedin_url") or e.linkedin_url
    e.setor = _txt(r, "setor") or e.setor
    func = _num(r.get("funcionarios"))
    # O número do LinkedIn é o mais exato e prevalece sobre Pipedrive/planilha, mas não sobre o informado
    # à mão: esse só é atualizado depois de FUNCIONARIOS_MANUAL_DIAS
    if func and not manual_em_vigor(e, quando):
        e.funcionarios, e.funcionarios_fonte, e.funcionarios_em = func, "linkedin", None
    if _txt(r, "site"):
        e.website = e.website or _txt(r, "site")
        e.dominio = e.dominio or dominio_site(_txt(r, "site"))
    if _txt(r, "sede") and not e.cidade:
        e.cidade, e.cidade_fonte = _txt(r, "sede"), "linkedin"
    if _txt(r, "descricao"):
        e.descricao = _txt(r, "descricao")[:2000]
    if isinstance(r.get("investida"), bool):
        e.investida = r["investida"]
    novos = 0
    for d in _lista(r.get("decisores"))[:20]:
        nome = d.get("nome") or d.get("name")
        if not nome:
            continue
        headline = d.get("headline") or d.get("cargo") or ""
        url = d.get("linkedin_url") or d.get("url") or None
        ja = db.scalar(select(Pessoa).where(Pessoa.empresa_id == e.id,
                                            Pessoa.nome_normalizado == normalizar_nome_pessoa(nome)))
        p = ja or resolver_pessoa(db, nome=nome, empresa=e, cargo=headline or None, linkedin_url=url, fonte="linkedin")
        p.linkedin_url = p.linkedin_url or url
        p.linkedin_headline = headline or p.linkedin_headline
        p.senioridade = p.senioridade or classificar_senioridade(headline)
        p.linkedin_em = quando
        novos += ja is None
    for post in _lista(r.get("posts"))[:5]:
        url, texto = post.get("url"), (post.get("texto") or post.get("text") or "").strip()
        if not url or not texto or db.scalar(select(Noticia.id).where(Noticia.empresa_id == e.id, Noticia.url == url)):
            continue
        db.add(Noticia(empresa_id=e.id, titulo=texto.splitlines()[0][:180], fonte="Post no LinkedIn", url=url,
                       publicada_em=_data(post.get("data") or post.get("date"))))
    e.linkedin_em = quando
    return novos


def _candidato_pessoa(p: Pessoa, candidatos: list[dict]) -> dict | None:
    """Mesmo nome (primeiro e último) e a empresa dele aparecendo no headline/empresa atual.
    Sem empresa conhecida não há como separar homônimos: não escolhe."""
    nossa = _termos(p.empresa.nome_fantasia or p.empresa.razao_social) if p.empresa else set()
    if not nossa:
        return None
    mesmos = [c for c in candidatos if mesmo_nome(p.nome, c.get("nome") or c.get("name") or "")]
    for c in mesmos:
        texto = " ".join(str(c.get(k) or "") for k in ("headline", "empresa_atual", "empresa"))
        if nossa & _termos(texto) and (c.get("linkedin_url") or c.get("url")):
            return c
    return None


def _escolher_pessoa(p: Pessoa, candidatos: list[dict]) -> str | None:
    c = _candidato_pessoa(p, candidatos)
    return (c.get("linkedin_url") or c.get("url")) if c else None


_UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR",
        "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}


def _no_brasil(local: str) -> bool:
    local = (local or "").strip()
    return bool(re.search(r"\b(brasil|brazil)\b", local, re.I)) or local[-2:].upper() in _UFS and \
        bool(re.search(r",\s*[A-Za-z]{2}$", local))


def _escolher_empresa(e: Empresa, candidatos: list[dict]) -> str | None:
    """Mesmo domínio do site; ou mesmo nome (sem sufixos societários) com um único candidato no Brasil.
    A busca de empresas não traz o site, então homônimos de outros países saem pelo local."""
    nome = normalizar_nome_empresa(e.nome_fantasia or re.sub(r"\(.*?\)", "", e.razao_social))
    validos = [c for c in candidatos if c.get("linkedin_url") or c.get("url")]
    url = lambda c: c.get("linkedin_url") or c.get("url")  # noqa: E731
    if e.dominio:
        for c in validos:
            if dominio_site(c.get("site") or c.get("website")) == e.dominio:
                return url(c)
    mesmos = [c for c in validos if nome and normalizar_nome_empresa(c.get("nome") or c.get("name") or "") == nome]
    if len(mesmos) > 1:
        mesmos = [c for c in mesmos if _no_brasil(c.get("local") or c.get("location") or "")]
    if len(mesmos) > 1 and e.uf:
        mesmos = [c for c in mesmos if (c.get("local") or c.get("location") or "").strip()[-2:].upper() == e.uf.upper()]
    return url(mesmos[0]) if len(mesmos) == 1 else None


def _aplicar_busca(obj, r: dict, quando: datetime) -> str:
    candidatos = _lista(r.get("candidatos"))
    obj.linkedin_busca_em = quando
    obj.linkedin_pedido_em = None  # a busca terminou: a leitura (se precisar) pode ser pedida já
    if isinstance(obj, Pessoa):
        c = _candidato_pessoa(obj, candidatos)
        if c:  # a busca já traz título e endereço: conta como leitura (a próxima só na validade)
            _aplicar_pessoa(obj, {"nome": c.get("nome") or c.get("name"), "headline": c.get("headline"),
                                  "linkedin_url": c.get("linkedin_url") or c.get("url")}, quando)
            obj.linkedin_nao_encontrado = False
            return "encontrados"
        url = None
    else:
        url = _escolher_empresa(obj, candidatos)
    if url:
        obj.linkedin_url = url
        obj.linkedin_nao_encontrado = False
        return "encontrados"
    obj.linkedin_nao_encontrado = True
    return "nao_encontrados"


_OUTRA_ORG = re.compile(r"(?:@|\bat\b|\bna\b|\bno\b)\s*([^|·,]+)$")  # "de"/"em" são parte do cargo


def _de_outra_empresa(headline: str, e: Empresa) -> bool:
    """O título cita outra organização (ex.: "Co-founder @ STAMINA VC" na lista da Salvy)?
    A lista de funcionários da Linked API traz também investidores e conselheiros."""
    nossa = _termos(e.nome_fantasia or e.razao_social) | _termos((e.linkedin_url or "").rsplit("/", 1)[-1].replace("-", " "))
    for parte in re.split(r"[|·]", headline):
        m = _OUTRA_ORG.search(parte.strip())
        if m and len(m.group(1).strip()) >= 3:
            org = _termos(m.group(1))
            if org and not org & nossa:
                return True
    return False


def _aplicar_area(db: Session, e: Empresa, r: dict, quando: datetime) -> int:
    """Funcionários com cargo de alguma área conhecida entram como contatos do LinkedIn.
    Quem não tem área no título ou cita outra organização fica de fora."""
    novos = 0
    for f in _lista(r.get("funcionarios_area"))[:50]:
        nome, headline = f.get("nome") or f.get("name"), f.get("headline") or ""
        if not nome or not classificar_area(headline) or _de_outra_empresa(headline, e):
            continue
        ja = db.scalar(select(Pessoa).where(Pessoa.empresa_id == e.id,
                                            Pessoa.nome_normalizado == normalizar_nome_pessoa(nome)))
        url = f.get("linkedin_url") or f.get("url") or None
        p = ja or resolver_pessoa(db, nome=nome, empresa=e, cargo=headline or None, linkedin_url=url, fonte="linkedin")
        p.linkedin_url = p.linkedin_url or url
        p.linkedin_headline = headline or p.linkedin_headline
        p.senioridade = p.senioridade or classificar_senioridade(headline)
        p.linkedin_em = quando
        novos += ja is None
    e.linkedin_areas = {**(e.linkedin_areas or {}), FUNCIONARIOS: quando.isoformat(timespec="seconds")}
    return novos


def receber(db: Session, resultados: list[dict]) -> dict:
    """Aplica resultados da Linked API (diretos ou devolvidos pelo n8n)."""
    cont = {"recebidos": 0, "pessoas": 0, "empresas": 0, "decisores_novos": 0, "nao_confirmado": 0, "erros": 0,
            "ja_lidos": 0, "encontrados": 0, "nao_encontrados": 0, "areas": 0, "contatos_area": 0}
    for r in resultados:
        cont["recebidos"] += 1
        alvo = _txt(r, "id_alvo")
        if _txt(r, "erro") or not alvo[1:].isdigit() or alvo[0] not in "PE":
            cont["erros"] += 1
            continue
        obj = db.get(Pessoa if alvo[0] == "P" else Empresa, int(alvo[1:]))
        if obj is None:
            cont["erros"] += 1
            continue
        quando = _data(r.get("capturado_em"))
        if _txt(r, "acao") == "buscar" or "candidatos" in r:
            cont[_aplicar_busca(obj, r, quando)] += 1
            continue
        if _txt(r, "acao") == "area" and isinstance(obj, Empresa):
            cont["contatos_area"] += _aplicar_area(db, obj, r, quando)
            cont["areas"] += 1
            continue
        if obj.linkedin_em and obj.linkedin_em >= quando:
            cont["ja_lidos"] += 1
            continue
        if isinstance(obj, Pessoa):
            cont[_aplicar_pessoa(obj, r, quando)] += 1
        else:
            cont["decisores_novos"] += _aplicar_empresa(db, obj, r, quando)
            cont["empresas"] += 1
    db.commit()
    return cont


def mudou_de_empresa(p: Pessoa) -> bool:
    if not p.linkedin_empresa_atual or not p.empresa:
        return False
    atual = set(normalizar_nome_empresa(p.linkedin_empresa_atual).split())
    nossa = set(normalizar_nome_empresa(p.empresa.nome_fantasia or p.empresa.razao_social).split())
    return bool(atual) and bool(nossa) and not (atual & nossa)


# --- Chamada direta da Linked API ------------------------------------------

PEDIDO_EXPIRA = timedelta(hours=24)


def _pedidos_24h(db: Session, agora: datetime) -> int:
    return len(db.scalars(select(LinkedinPedido.id).where(LinkedinPedido.criado_em > agora - timedelta(hours=24))).all())


def _cliente(settings: Settings):
    from crosssell.connectors.linkedapi import LinkedApiClient

    return LinkedApiClient(settings.linked_api_token, settings.linked_api_identification_token)


def coletar(db: Session, client, agora: datetime | None = None) -> dict:
    """Confere os pedidos em andamento e aplica os que terminaram."""
    from crosssell.connectors.linkedapi import LinkedApiErro, resultado

    agora = agora or datetime.utcnow()
    cont = {"aplicados": 0, "andamento": 0, "erros": 0, "expirados": 0}
    for ped in db.scalars(select(LinkedinPedido).where(LinkedinPedido.situacao == "pendente")).all():
        try:
            st = client.consultar(ped.workflow_id)
        except LinkedApiErro as exc:
            if exc.tipo == "conexao":  # sem resposta não dá para saber se terminou: confere na próxima rodada
                cont["andamento"] += 1
                continue
            st = {"workflowStatus": "failed", "failure": {"message": str(exc)}}
        status = st.get("workflowStatus")
        if status in ("pending", "running"):
            if ped.criado_em < agora - PEDIDO_EXPIRA:
                ped.situacao, ped.concluido_em = "expirado", agora
                cont["expirados"] += 1
            else:
                cont["andamento"] += 1
            continue
        tipo = "pessoa" if ped.id_alvo[:1] == "P" else "empresa"
        r = resultado(ped.id_alvo, tipo, ped.acao, ped.area, st.get("completion")) if status == "completed" else \
            {"id_alvo": ped.id_alvo, "erro": (st.get("failure") or {}).get("message") or "falhou"}
        r["capturado_em"] = agora.isoformat(timespec="seconds")
        ped.concluido_em = agora
        if r.get("erro"):
            ped.situacao, ped.erro = "erro", str(r["erro"])[:500]
            cont["erros"] += 1
            continue
        receber(db, [r])
        ped.situacao = "aplicado"
        cont["aplicados"] += 1
    db.commit()
    return cont


def iniciar(db: Session, settings: Settings, client, agora: datetime | None = None) -> dict:
    """Inicia os próximos pedidos, respeitando o lote por rodada e o limite de 24 h."""
    from crosssell.connectors.linkedapi import LinkedApiErro, definicao

    agora = agora or datetime.utcnow()
    vagas = min(settings.linkedin_lote, settings.linkedin_limite_dia - _pedidos_24h(db, agora))
    cont = {"iniciados": 0, "falhas": 0, "limite": vagas <= 0}
    if vagas <= 0:
        return cont
    for a in alvos(db, settings, agora):
        if cont["iniciados"] >= vagas:
            break
        try:
            wid = client.iniciar(definicao(a))
        except LinkedApiErro as exc:
            cont["falhas"] += 1
            cont["erro"] = str(exc)
            if "limit" in exc.tipo.lower() or cont["falhas"] >= 3:
                break  # limite da conta ou falha repetida: tenta na próxima rodada
            continue
        db.add(LinkedinPedido(workflow_id=wid, id_alvo=a["id_alvo"], acao=a["acao"], area=a.get("area"), criado_em=agora))
        obj = db.get(Pessoa if a["tipo"] == "pessoa" else Empresa, int(a["id_alvo"][1:]))
        if a["acao"] == "area":
            obj.linkedin_areas = {**(obj.linkedin_areas or {}), FUNCIONARIOS: agora.isoformat(timespec="seconds")}
        else:
            obj.linkedin_pedido_em = agora
        db.commit()  # já foi pago: grava na hora, para uma falha adiante não fazer pedir de novo
        cont["iniciados"] += 1
    db.commit()
    return cont


def executar(db: Session, settings: Settings, client=None, agora: datetime | None = None) -> dict:
    """Uma rodada: aplica o que terminou e inicia os próximos."""
    if not direto(settings):
        raise RuntimeError("Linked API não configurada (LINKED_API_TOKEN e LINKED_API_IDENTIFICATION_TOKEN).")
    client = client or _cliente(settings)
    return {**coletar(db, client, agora), **iniciar(db, settings, client, agora)}
