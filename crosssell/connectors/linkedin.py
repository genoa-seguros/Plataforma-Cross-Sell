"""LinkedIn via n8n + Linked API, sem planilha intermediária.

Fluxo automático:
  1. A plataforma levanta os alvos: empresas e contatos dos negócios abertos que
     nunca foram lidos no LinkedIn ou cuja leitura passou da validade.
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
from crosssell.models import Empresa, Noticia, Pessoa
from crosssell.normalize import classificar_senioridade, dominio_site, normalizar_nome_empresa, normalizar_nome_pessoa
from crosssell.resolver import resolver_pessoa

PRAZO_PEDIDO = timedelta(hours=36)
CAMPOS_RESULTADO = ("id_alvo", "tipo", "linkedin_url", "nome", "headline", "cargo_atual", "empresa_atual",
                    "localizacao", "setor", "funcionarios", "site", "sede", "decisores", "posts", "capturado_em", "erro")


def configurado(settings: Settings) -> bool:
    return bool(settings.n8n_linkedin_webhook_url and settings.n8n_token and settings.plataforma_url)


def token_valido(settings: Settings, cabecalho: str | None) -> bool:
    esperado = f"Bearer {settings.n8n_token}"
    return bool(settings.n8n_token) and secrets.compare_digest((cabecalho or "").encode(), esperado.encode())


# --- 1. Alvos --------------------------------------------------------------

def _precisa(lido_em: datetime | None, pedido_em: datetime | None, validade_dias: int, agora: datetime) -> bool:
    if pedido_em and pedido_em > agora - PRAZO_PEDIDO and (lido_em is None or lido_em < pedido_em):
        return False  # já foi pedido e ainda não voltou
    return lido_em is None or lido_em < agora - timedelta(days=validade_dias)


def alvos(db: Session, settings: Settings, agora: datetime | None = None) -> list[dict]:
    from crosssell import tabela

    agora = agora or datetime.utcnow()
    validade = settings.linkedin_validade_dias
    empresas: dict[int, str] = {}
    pessoas: dict[int, str] = {}
    for linha in tabela.montar(db, settings):
        if linha["empresa"]:
            empresas.setdefault(linha["empresa"]["id"], f"{linha['funil']}: {linha['titulo']}")
        if linha["pessoa"]:
            pessoas.setdefault(linha["pessoa"]["id"], "contato do negócio")
        for c in linha["contatos"]:
            pessoas.setdefault(c["id"], "contato da empresa")

    saida = []
    for e in db.scalars(select(Empresa).where(Empresa.id.in_(empresas)).order_by(Empresa.id)):
        if _precisa(e.linkedin_em, e.linkedin_pedido_em, validade, agora):
            saida.append({"id_alvo": f"E{e.id}", "tipo": "empresa", "nome": e.nome_fantasia or e.razao_social,
                          "empresa": e.razao_social, "cargo": "", "email": "", "linkedin_url": e.linkedin_url or "",
                          "site": e.website or (f"https://{e.dominio}" if e.dominio else ""), "cnpj": e.cnpj or "",
                          "motivo": empresas[e.id]})
    for p in db.scalars(select(Pessoa).where(Pessoa.id.in_(pessoas)).order_by(Pessoa.id)):
        if _precisa(p.linkedin_em, p.linkedin_pedido_em, validade, agora):
            saida.append({"id_alvo": f"P{p.id}", "tipo": "pessoa", "nome": p.nome,
                          "empresa": p.empresa.razao_social if p.empresa else "", "cargo": p.cargo or "",
                          "email": p.email or "", "linkedin_url": p.linkedin_url or "", "site": "", "cnpj": "",
                          "motivo": pessoas[p.id]})
    return saida


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
    return {"enviados": len(enviados), "falhas": falhas, "empresas": sum(a["tipo"] == "empresa" for a in enviados),
            "pessoas": sum(a["tipo"] == "pessoa" for a in enviados), "lote": lote_id}


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


def _aplicar_empresa(db: Session, e: Empresa, r: dict, quando: datetime) -> int:
    e.linkedin_url = _txt(r, "linkedin_url") or e.linkedin_url
    e.setor = _txt(r, "setor") or e.setor
    e.funcionarios = _num(r.get("funcionarios")) or e.funcionarios
    if _txt(r, "site"):
        e.website = e.website or _txt(r, "site")
        e.dominio = e.dominio or dominio_site(_txt(r, "site"))
    if _txt(r, "sede") and not e.cidade:
        e.cidade = _txt(r, "sede")
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


def receber(db: Session, resultados: list[dict]) -> dict:
    """Aplica os resultados que o n8n devolveu (um ou vários por chamada)."""
    cont = {"recebidos": 0, "pessoas": 0, "empresas": 0, "decisores_novos": 0, "nao_confirmado": 0, "erros": 0,
            "ja_lidos": 0}
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
