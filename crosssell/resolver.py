"""Resolução de entidades: encontra (ou cria) a mesma Empresa/Pessoa vinda de fontes diferentes.

Ordem de confiança para empresas: CNPJ > id do Pipedrive > domínio > nome normalizado.
Para pessoas: CPF > e-mail > id do Pipedrive > (nome + empresa).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.models import Empresa, Pessoa
from crosssell.normalize import (
    classificar_senioridade,
    dominio_email,
    dominio_site,
    normalizar_cnpj,
    normalizar_cpf,
    normalizar_email,
    normalizar_nome_empresa,
    normalizar_nome_pessoa,
)


def _preencher(obj, **campos) -> None:
    """Preenche apenas campos vazios — nunca sobrescreve dado existente com vazio."""
    for k, v in campos.items():
        if v not in (None, "") and getattr(obj, k) in (None, ""):
            setattr(obj, k, v)


def resolver_empresa(
    db: Session,
    *,
    razao_social: str | None = None,
    cnpj: str | None = None,
    pipedrive_org_id: int | None = None,
    dominio: str | None = None,
    website: str | None = None,
    criar: bool = True,
    **extras,
) -> Empresa | None:
    cnpj = normalizar_cnpj(cnpj) if cnpj else None
    dominio = dominio or dominio_site(website)
    nome_norm = normalizar_nome_empresa(razao_social) if razao_social else None

    empresa = None
    if cnpj:
        empresa = db.scalar(select(Empresa).where(Empresa.cnpj == cnpj))
    if empresa is None and pipedrive_org_id:
        empresa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == pipedrive_org_id))
    if empresa is None and dominio:
        empresa = db.scalar(select(Empresa).where(Empresa.dominio == dominio))
    if empresa is None and nome_norm:
        candidatos = db.scalars(select(Empresa).where(Empresa.nome_normalizado == nome_norm)).all()
        # Nome só é usado se não houver conflito de CNPJ.
        candidatos = [c for c in candidatos if not (cnpj and c.cnpj and c.cnpj != cnpj)]
        if len(candidatos) == 1:
            empresa = candidatos[0]

    if empresa is None:
        if not criar or not razao_social:
            return None
        empresa = Empresa(razao_social=razao_social.strip(), nome_normalizado=nome_norm or "")
        db.add(empresa)

    _preencher(
        empresa, cnpj=cnpj, pipedrive_org_id=pipedrive_org_id, dominio=dominio,
        website=website, **extras,
    )
    db.flush()
    return empresa


def resolver_pessoa(
    db: Session,
    *,
    nome: str | None = None,
    email: str | None = None,
    cpf: str | None = None,
    empresa: Empresa | None = None,
    pipedrive_person_id: int | None = None,
    cargo: str | None = None,
    fonte: str = "manual",
    criar: bool = True,
    **extras,
) -> Pessoa | None:
    email = normalizar_email(email)
    cpf = normalizar_cpf(cpf) if cpf else None
    nome_norm = normalizar_nome_pessoa(nome) if nome else None

    pessoa = None
    if cpf:
        pessoa = db.scalar(select(Pessoa).where(Pessoa.cpf == cpf))
    if pessoa is None and email:
        pessoa = db.scalar(select(Pessoa).where(Pessoa.email == email))
    if pessoa is None and pipedrive_person_id:
        pessoa = db.scalar(select(Pessoa).where(Pessoa.pipedrive_person_id == pipedrive_person_id))
    linkedin_url = extras.get("linkedin_url")
    if pessoa is None and linkedin_url:
        pessoa = db.scalar(select(Pessoa).where(Pessoa.linkedin_url == linkedin_url))
    if pessoa is None and nome_norm and empresa is not None and empresa.id:
        pessoa = db.scalar(
            select(Pessoa).where(Pessoa.nome_normalizado == nome_norm, Pessoa.empresa_id == empresa.id)
        )

    if pessoa is None:
        if not criar:
            return None
        nome = nome or (email.split("@")[0] if email else None)
        if not nome:
            return None
        pessoa = Pessoa(nome=nome.strip(), nome_normalizado=normalizar_nome_pessoa(nome), fonte=fonte)
        db.add(pessoa)

    if empresa is None and email and pessoa.empresa_id is None:
        dom = dominio_email(email)
        if dom:
            empresa = db.scalar(select(Empresa).where(Empresa.dominio == dom))

    _preencher(
        pessoa, email=email, cpf=cpf, pipedrive_person_id=pipedrive_person_id, cargo=cargo,
        empresa_id=empresa.id if empresa is not None else None, **extras,
    )
    if pessoa.cargo and not pessoa.senioridade:
        pessoa.senioridade = classificar_senioridade(pessoa.cargo)
    db.flush()
    return pessoa
