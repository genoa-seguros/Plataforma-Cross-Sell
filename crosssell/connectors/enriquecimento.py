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

from datetime import datetime
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.connectors.planilhas import ler_linhas, mapear
from crosssell.models import Empresa
from crosssell.normalize import normalizar_cnpj
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
    empresa.cnae = f"{d.get('cnae_fiscal')} - {d.get('cnae_fiscal_descricao')}" if d.get("cnae_fiscal") else empresa.cnae
    empresa.porte = d.get("porte") or empresa.porte
    empresa.capital_social = d.get("capital_social") or empresa.capital_social
    empresa.cidade = d.get("municipio") or empresa.cidade
    empresa.uf = d.get("uf") or empresa.uf
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
        if r.get("funcionarios") and not empresa.funcionarios:
            digitos = "".join(c for c in str(r["funcionarios"]).split("-")[-1] if c.isdigit())
            empresa.funcionarios = int(digitos) if digitos else None
        nome = " ".join(x for x in (r.get("nome"), r.get("sobrenome")) if x)
        if nome:
            resolver_pessoa(db, nome=nome, email=r.get("email"), empresa=empresa, cargo=r.get("cargo"),
                            linkedin_url=r.get("linkedin"), fonte="linkedin")
            cont["pessoas"] += 1
    db.commit()
    return cont


def importar_linkedin(db: Session, settings: Settings, conteudo: bytes, nome_arquivo: str) -> dict:
    return registrar_pessoas(db, [mapear(lin, MAPA_LINKEDIN) for lin in ler_linhas(conteudo, nome_arquivo)])
