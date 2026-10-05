"""Principais notícias de cada empresa, via RSS do Google Notícias (pt-BR).

Busca pelo nome fantasia (ou razão social sem sufixos) entre aspas e mantém só
manchetes que citam o nome. Roda para as empresas que aparecem na tabela.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.models import Empresa, Noticia
from crosssell.normalize import normalizar_nome_empresa, sem_acento

RSS = "https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
JANELA_DIAS = 180
MAX_POR_EMPRESA = 5


def termo_busca(e: Empresa) -> str:
    base = e.nome_fantasia or e.razao_social
    base = re.sub(r"\(.*?\)", "", base)
    return normalizar_nome_empresa(base) if not e.nome_fantasia else base.strip()


def _cita(titulo: str, termo: str) -> bool:
    t = sem_acento(titulo).lower()
    return all(tok in t for tok in sem_acento(termo).lower().split()[:3])


def ler_rss(xml: str) -> list[dict]:
    itens = []
    for item in ET.fromstring(xml).iter("item"):
        titulo = (item.findtext("title") or "").strip()
        fonte = item.findtext("source")
        # O Google acrescenta " - Fonte" ao fim do título.
        if fonte and titulo.endswith(f" - {fonte}"):
            titulo = titulo[: -len(fonte) - 3]
        try:
            data = parsedate_to_datetime(item.findtext("pubDate") or "").replace(tzinfo=None)
        except (TypeError, ValueError):
            data = None
        itens.append({"titulo": titulo, "fonte": fonte, "url": item.findtext("link") or "", "data": data})
    return itens


def atualizar_empresa(db: Session, e: Empresa, http: httpx.Client) -> int:
    termo = termo_busca(e)
    if len(termo) < 3:
        return 0
    r = http.get(RSS.format(q=quote_plus(f'"{termo}"')))
    if r.status_code != 200:
        return 0
    limite = datetime.utcnow() - timedelta(days=JANELA_DIAS)
    novos = 0
    itens = [i for i in ler_rss(r.text) if i["url"] and _cita(i["titulo"], termo) and (not i["data"] or i["data"] >= limite)]
    for i in sorted(itens, key=lambda i: i["data"] or datetime.min, reverse=True)[:MAX_POR_EMPRESA]:
        if db.scalar(select(Noticia.id).where(Noticia.empresa_id == e.id, Noticia.url == i["url"])):
            continue
        db.add(Noticia(empresa_id=e.id, titulo=i["titulo"], fonte=i["fonte"], url=i["url"], publicada_em=i["data"]))
        novos += 1
    e.noticias_em = datetime.utcnow()
    return novos


def atualizar(db: Session, empresa_ids: list[int], http: httpx.Client | None = None, horas: int = 24) -> dict:
    http = http or httpx.Client(timeout=20, follow_redirects=True)
    corte = datetime.utcnow() - timedelta(hours=horas)
    cont = {"empresas": 0, "noticias": 0}
    for e in db.scalars(select(Empresa).where(Empresa.id.in_(empresa_ids))):
        if e.noticias_em and e.noticias_em > corte:
            continue
        cont["noticias"] += atualizar_empresa(db, e, http)
        cont["empresas"] += 1
    db.commit()
    return cont
