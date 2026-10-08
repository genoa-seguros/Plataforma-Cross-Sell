"""Enriquecimento de empresas e pessoas.

1. Receita Federal (via BrasilAPI, gratuito): porte, CNAE, capital social,
   cidade/UF e o quadro societário (QSA) — os sócios viram Pessoa com
   senioridade "socio", candidatos naturais a Linhas Pessoais.

2. LinkedIn: a API oficial do LinkedIn não expõe busca de funcionários de
   terceiros, e raspagem viola os termos de uso. Por isso o conector aceita a
   exportação de listas do Sales Navigator (ou de um provedor licenciado de
   dados B2B) em CSV/XLSX, com colunas como nome, cargo, empresa, e-mail,
   linkedin. A interface ProvedorPessoas permite plugar um provedor via API.
"""

import re
from datetime import datetime, timedelta
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.connectors.planilhas import ler_linhas, mapear
from crosssell.models import Empresa
from crosssell.normalize import cnpj_valido, dominio_site, normalizar_cnpj, normalizar_nome_empresa, so_digitos
from crosssell.resolver import resolver_empresa, resolver_pessoa

BRASILAPI = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"

MAPA_LINKEDIN = {
    "nome": ["nome", "name", "full name", "first name"],
    "sobrenome": ["sobrenome", "last name"],
    "cargo": ["cargo", "title", "job title", "position"],
    "empresa": ["empresa", "company", "company name", "account name"],
    "cnpj": ["cnpj"],
    "email": ["email", "e-mail", "email address"],
    "linkedin": ["linkedin", "linkedin url", "profile url", "person linkedin url"],
    "empresa_linkedin": ["company linkedin url", "empresa linkedin"],
    "site": ["website", "company website", "site"],
    "funcionarios": ["employees", "company size", "funcionarios", "# employees"],
    "setor": ["industry", "setor"],
}


class ProvedorPessoas(Protocol):
    def pessoas_da_empresa(self, empresa: Empresa) -> list[dict]:
        """Retorna dicts com chaves de MAPA_LINKEDIN."""


def enriquecer_receita(db: Session, empresa: Empresa, http: httpx.Client | None = None) -> bool:
    if not empresa.cnpj:
        return False
    http = http or httpx.Client(timeout=20)
    r = http.get(BRASILAPI.format(cnpj=empresa.cnpj))
    if r.status_code != 200:
        return False
    d = r.json()
    empresa.nome_fantasia = empresa.nome_fantasia or d.get("nome_fantasia") or None
    empresa.razao_receita = d.get("razao_social") or empresa.razao_receita
    empresa.cnae = f"{d.get('cnae_fiscal')} - {d.get('cnae_fiscal_descricao')}" if d.get("cnae_fiscal") else empresa.cnae
    empresa.porte = d.get("porte") or empresa.porte
    if d.get("natureza_juridica"):
        empresa.natureza_juridica = f"{d.get('codigo_natureza_juridica') or ''} - {d['natureza_juridica']}".strip(" -")
    empresa.capital_social = d.get("capital_social") or empresa.capital_social
    if empresa.cidade_fonte != "manual" and d.get("municipio"):  # a cidade da matriz; a informada à mão vale mais
        empresa.cidade, empresa.uf, empresa.cidade_fonte = d["municipio"], d.get("uf") or empresa.uf, "receita"
    email = (d.get("email") or "").strip().lower()
    if "@" in email and dominio_combina(empresa, email.split("@", 1)[1]):  # muitas vezes é o e-mail do contador
        adicionar_dominio(empresa, email.split("@", 1)[1])
    for socio in d.get("qsa") or []:
        if socio.get("nome_socio"):
            p = resolver_pessoa(db, nome=socio["nome_socio"].title(), empresa=empresa,
                                cargo=socio.get("qualificacao_socio"), fonte="receita")
            p.senioridade = "socio"
    empresa.enriquecido_em = datetime.utcnow()
    db.flush()
    return True


def enriquecer_todas(db: Session, limite: int = 200, http: httpx.Client | None = None) -> dict:
    pendentes = db.scalars(
        select(Empresa).where(Empresa.cnpj.is_not(None), Empresa.enriquecido_em.is_(None)).limit(limite)
    ).all()
    ok = sum(enriquecer_receita(db, e, http) for e in pendentes)
    db.commit()
    return {"pendentes": len(pendentes), "enriquecidas": ok}


def registrar_pessoas(db: Session, linhas: list[dict]) -> dict:
    cont = {"linhas": 0, "pessoas": 0, "sem_empresa": 0}
    for r in linhas:
        cont["linhas"] += 1
        empresa = resolver_empresa(
            db,
            razao_social=r.get("empresa"),
            cnpj=normalizar_cnpj(r.get("cnpj")) if r.get("cnpj") else None,
            website=r.get("site"),
            linkedin_url=r.get("empresa_linkedin"),
            setor=r.get("setor"),
        )
        if empresa is None:
            cont["sem_empresa"] += 1
            continue
        if r.get("setor") and empresa.setor_fonte != "manual":  # planilha exportada do LinkedIn
            empresa.setor, empresa.setor_fonte = r["setor"], "linkedin"
        if r.get("funcionarios") and empresa.funcionarios_fonte != "manual":
            digitos = "".join(c for c in str(r["funcionarios"]).split("-")[-1] if c.isdigit())
            if digitos:
                empresa.funcionarios, empresa.funcionarios_fonte = int(digitos), "linkedin"
        nome = " ".join(x for x in (r.get("nome"), r.get("sobrenome")) if x)
        if nome:
            resolver_pessoa(db, nome=nome, email=r.get("email"), empresa=empresa, cargo=r.get("cargo"),
                            linkedin_url=r.get("linkedin"), fonte="linkedin")
            cont["pessoas"] += 1
    db.commit()
    return cont


def importar_linkedin(db: Session, settings: Settings, conteudo: bytes, nome_arquivo: str) -> dict:
    return registrar_pessoas(db, [mapear(lin, MAPA_LINKEDIN) for lin in ler_linhas(conteudo, nome_arquivo)])


# --- CNPJ no site da empresa ----------------------------------------------------------
# Quase todo site brasileiro traz o CNPJ no rodapé. Achado o CNPJ, a Receita dá a cidade da matriz, porte,
# CNAE e sócios, sem gastar consulta do LinkedIn. Só é aceito se o nome na Receita bater com o da empresa
# (o rodapé pode trazer o CNPJ da agência que fez o site, por exemplo).
_CNPJ_NO_TEXTO = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_PALAVRAS_GENERICAS = {"ltda", "eireli", "servicos", "comercio", "industria", "brasil", "grupo", "solucoes",
                       "tecnologia", "consultoria", "participacoes", "holding", "empresa", "companhia"}


def cnpjs_no_html(html: str) -> list[str]:
    """CNPJs válidos (dígitos verificadores) que aparecem no texto, na ordem, sem repetir."""
    vistos = []
    for bruto in _CNPJ_NO_TEXTO.findall(html or ""):
        c = so_digitos(bruto)
        if len(c) == 14 and cnpj_valido(c) and c not in vistos:
            vistos.append(c)
    return vistos


def _termos(texto: str | None) -> set[str]:
    return {t for t in normalizar_nome_empresa(texto or "").split() if len(t) >= 4 and t not in _PALAVRAS_GENERICAS}


def mesma_empresa(e: Empresa, dados: dict) -> bool:
    """O nome na Receita (razão social ou fantasia) tem alguma palavra marcante do nome ou do domínio da empresa."""
    nossos = _termos(e.razao_social) | _termos(e.nome_fantasia) | _termos((e.dominio or "").split(".")[0])
    deles = _termos(dados.get("razao_social")) | _termos(dados.get("nome_fantasia"))
    return bool(nossos & deles)


def cnpj_por_site(db: Session, empresa_ids: list[int], http: httpx.Client | None = None, limite: int = 50) -> dict:
    """Procura o CNPJ no site das empresas sem CNPJ (as da tabela e das Oportunidades), uma vez por empresa."""
    http = http or httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (CrossSell)"})
    cont = {"verificadas": 0, "encontradas": 0}
    pendentes = db.scalars(select(Empresa).where(Empresa.id.in_(empresa_ids), Empresa.cnpj.is_(None),
                                                Empresa.dominio.is_not(None), Empresa.site_cnpj_em.is_(None))
                           .limit(limite)).all()
    for e in pendentes:
        e.site_cnpj_em = datetime.utcnow()
        cont["verificadas"] += 1
        achados = []
        for url in (f"https://{e.dominio}", f"https://www.{e.dominio}"):
            try:
                r = http.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                achados = cnpjs_no_html(r.text)
                break
        for cnpj in achados[:3]:
            try:
                r = http.get(BRASILAPI.format(cnpj=cnpj))
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and mesma_empresa(e, r.json()):
                e.cnpj = cnpj
                enriquecer_receita(db, e, http)
                cont["encontradas"] += 1
                break
        db.commit()
    return cont


# --- Domínios de e-mail da empresa --------------------------------------------

_EMAIL_NO_TEXTO = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_EXTENSOES_ARQUIVO = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
DOMINIOS_VALIDADE_DIAS = 180


def adicionar_dominio(e: Empresa, dominio: str | None, internos: set[str] = frozenset()) -> bool:
    """Guarda um domínio de e-mail a mais da empresa (o do site fica em Empresa.dominio)."""
    from crosssell.normalize import DOMINIOS_PUBLICOS

    dom = (dominio or "").lower().strip().removeprefix("www.")
    if not dom or "." not in dom or dom == e.dominio or dom in DOMINIOS_PUBLICOS or dom in internos \
            or dom in (e.dominios_extras or []) or dom.endswith(_EXTENSOES_ARQUIVO):
        return False
    e.dominios_extras = [*(e.dominios_extras or []), dom][:5]
    return True


def dominios_no_html(html: str) -> list[str]:
    """Domínios dos e-mails que aparecem na página (contato@..., mailto:), sem repetir, na ordem."""
    vistos: list[str] = []
    for m in _EMAIL_NO_TEXTO.finditer(html or ""):
        dom = m.group(1).lower().rstrip(".")
        if dom not in vistos and not dom.endswith(_EXTENSOES_ARQUIVO):
            vistos.append(dom)
    return vistos


def dominio_combina(e: Empresa, dominio: str) -> bool:
    """O domínio lembra o nome da empresa? (para o e-mail da Receita, que muitas vezes é o do contador)"""
    raiz = normalizar_nome_empresa(dominio.split(".")[0]).replace(" ", "")
    nomes = [normalizar_nome_empresa(x or "") for x in (e.razao_social, e.nome_fantasia, e.razao_receita)]
    termos = {t for n in nomes for t in n.split() if len(t) >= 4 and t not in _PALAVRAS_GENERICAS}
    return len(raiz) >= 4 and (any(t in raiz for t in termos) or any(raiz in n.replace(" ", "") for n in nomes if n))


def descobrir_dominios(db: Session, settings, empresa_ids: list[int], http: httpx.Client | None = None,
                       limite: int = 50) -> dict:
    """Outros domínios de e-mail de cada empresa, de graça e uma vez a cada 180 dias:
    para onde o site redireciona e os e-mails que aparecem na página. (O site do LinkedIn e o e-mail da Receita
    entram quando cada leitura acontece.) Com o domínio novo, a leitura de e-mails religa as mensagens já lidas."""
    http = http or httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (CrossSell)"})
    internos = settings.internal_domain_set
    cont = {"verificadas": 0, "dominios_novos": 0}
    corte = datetime.utcnow() - timedelta(days=DOMINIOS_VALIDADE_DIAS)
    pendentes = db.scalars(select(Empresa).where(
        Empresa.id.in_(empresa_ids), Empresa.dominio.is_not(None),
        (Empresa.dominios_em.is_(None)) | (Empresa.dominios_em < corte)).limit(limite)).all()
    for e in pendentes:
        e.dominios_em = datetime.utcnow()
        cont["verificadas"] += 1
        for url in (f"https://{e.dominio}", f"https://www.{e.dominio}"):
            try:
                r = http.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            achados = [dominio_site(str(r.url))] + dominios_no_html(r.text)[:5]
            cont["dominios_novos"] += sum(adicionar_dominio(e, d, internos) for d in achados)
            break
        db.commit()
    return cont
