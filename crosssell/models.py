"""Modelo de dados unificado.

A chave de ligação entre as verticais é a Empresa (identificada por CNPJ,
com fallback para domínio de e-mail e nome normalizado) e a Pessoa
(identificada por e-mail ou CPF). Tudo que é "produto" em qualquer vertical
— negócio no Pipedrive, apólice de saúde no Zeca ou apólice PF no Quiver —
vira um registro de Negocio.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from crosssell.db import Base


def _now() -> datetime:
    return datetime.utcnow()


class Empresa(Base):
    __tablename__ = "empresas"

    id: Mapped[int] = mapped_column(primary_key=True)
    cnpj: Mapped[str | None] = mapped_column(String(14), unique=True, index=True)
    razao_social: Mapped[str]
    nome_fantasia: Mapped[str | None]
    nome_normalizado: Mapped[str] = mapped_column(index=True)
    dominio: Mapped[str | None] = mapped_column(index=True)
    website: Mapped[str | None]
    linkedin_url: Mapped[str | None]
    setor: Mapped[str | None]
    cnae: Mapped[str | None]
    porte: Mapped[str | None]
    funcionarios: Mapped[int | None]
    capital_social: Mapped[float | None]
    cidade: Mapped[str | None]
    uf: Mapped[str | None]
    pipedrive_org_id: Mapped[int | None] = mapped_column(index=True)
    enriquecido_em: Mapped[datetime | None]

    score_relacionamento: Mapped[float] = mapped_column(Float, default=0.0)
    score_componentes: Mapped[dict] = mapped_column(JSON, default=dict)

    pessoas: Mapped[list["Pessoa"]] = relationship(back_populates="empresa")
    negocios: Mapped[list["Negocio"]] = relationship(back_populates="empresa")


class Pessoa(Base):
    __tablename__ = "pessoas"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int | None] = mapped_column(ForeignKey("empresas.id"), index=True)
    nome: Mapped[str]
    nome_normalizado: Mapped[str] = mapped_column(index=True)
    email: Mapped[str | None] = mapped_column(unique=True, index=True)
    cpf: Mapped[str | None] = mapped_column(String(11), unique=True, index=True)
    cargo: Mapped[str | None]
    telefone: Mapped[str | None]
    linkedin_url: Mapped[str | None]
    pipedrive_person_id: Mapped[int | None] = mapped_column(index=True)
    # origem do cadastro: pipedrive | quiver | email | receita (QSA) | linkedin
    fonte: Mapped[str] = mapped_column(default="manual")
    # socio | c_level | diretor | gerente | outro
    senioridade: Mapped[str | None]

    score_relacionamento: Mapped[float] = mapped_column(Float, default=0.0)
    score_componentes: Mapped[dict] = mapped_column(JSON, default=dict)
    ponto_focal: Mapped[bool] = mapped_column(default=False)

    empresa: Mapped[Empresa | None] = relationship(back_populates="pessoas")
    negocios: Mapped[list["Negocio"]] = relationship(back_populates="pessoa")


class UsuarioInterno(Base):
    __tablename__ = "usuarios_internos"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
    nome: Mapped[str]
    verticais: Mapped[list] = mapped_column(JSON, default=list)
    lider: Mapped[list] = mapped_column(JSON, default=list)


class Negocio(Base):
    """Um produto/relacionamento comercial em uma vertical."""

    __tablename__ = "negocios"
    __table_args__ = (UniqueConstraint("fonte", "id_externo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int | None] = mapped_column(ForeignKey("empresas.id"), index=True)
    pessoa_id: Mapped[int | None] = mapped_column(ForeignKey("pessoas.id"), index=True)
    vertical: Mapped[str] = mapped_column(index=True)
    fonte: Mapped[str]  # pipedrive | zeca | quiver
    id_externo: Mapped[str]
    titulo: Mapped[str | None]
    # aberto | ganho | perdido (pipeline) ; ativo | cancelado (apólices)
    status: Mapped[str] = mapped_column(index=True)
    produto: Mapped[str | None]
    seguradora: Mapped[str | None]
    inicio_vigencia: Mapped[date | None] = mapped_column(Date)
    fim_vigencia: Mapped[date | None] = mapped_column(Date)
    valor: Mapped[float | None]
    vidas: Mapped[int | None]
    responsavel_email: Mapped[str | None]
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    empresa: Mapped[Empresa | None] = relationship(back_populates="negocios")
    pessoa: Mapped[Pessoa | None] = relationship(back_populates="negocios")

    @property
    def vigente(self) -> bool:
        """Cliente de fato: negócio ganho/apólice ativa e ainda dentro da vigência.

        Negócio aberto no funil NÃO torna a empresa cliente.
        """
        if self.status not in ("ganho", "ativo"):
            return False
        hoje = date.today()
        if self.fim_vigencia:
            return self.fim_vigencia >= hoje
        if self.fonte == "pipedrive" and self.inicio_vigencia:
            # Sem fim de vigência informado: assume apólice anual (+1 mês de tolerância).
            return self.inicio_vigencia >= hoje - timedelta(days=395)
        return True

    @property
    def ex_cliente(self) -> bool:
        """Já foi cliente nesta vertical (vigência vencida ou apólice cancelada)."""
        return self.status == "cancelado" or (self.status in ("ganho", "ativo") and not self.vigente)


class Interacao(Base):
    """Metadado de uma troca de e-mail entre um usuário interno e um contato externo.

    Por LGPD e por economia, guardamos apenas metadados (quem, quando, direção,
    thread). Assunto e corpo não são armazenados.
    """

    __tablename__ = "interacoes"
    __table_args__ = (UniqueConstraint("message_id", "email_externo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str]
    thread_id: Mapped[str | None] = mapped_column(index=True)
    data: Mapped[datetime] = mapped_column(index=True)
    usuario_email: Mapped[str] = mapped_column(index=True)
    email_externo: Mapped[str] = mapped_column(index=True)
    direcao: Mapped[str]  # enviado | recebido
    pessoa_id: Mapped[int | None] = mapped_column(ForeignKey("pessoas.id"), index=True)
    empresa_id: Mapped[int | None] = mapped_column(ForeignKey("empresas.id"), index=True)


class Oportunidade(Base):
    __tablename__ = "oportunidades"
    __table_args__ = (UniqueConstraint("empresa_id", "pessoa_id", "vertical_alvo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int | None] = mapped_column(ForeignKey("empresas.id"), index=True)
    pessoa_id: Mapped[int | None] = mapped_column(ForeignKey("pessoas.id"), index=True)
    vertical_alvo: Mapped[str] = mapped_column(index=True)
    verticais_atuais: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[float] = mapped_column(index=True)
    componentes: Mapped[dict] = mapped_column(JSON, default=dict)
    motivos: Mapped[list] = mapped_column(JSON, default=list)
    ponte_email: Mapped[str | None]  # usuário interno com melhor relação para apresentar
    responsavel_email: Mapped[str | None]  # líder/equipe da vertical-alvo que vai atender
    # nova | em_andamento | convertida | descartada
    status: Mapped[str] = mapped_column(default="nova")
    calculado_em: Mapped[datetime] = mapped_column(DateTime, default=_now)

    empresa: Mapped[Empresa | None] = relationship()
    pessoa: Mapped[Pessoa | None] = relationship()


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    fonte: Mapped[str]
    inicio: Mapped[datetime] = mapped_column(default=_now)
    fim: Mapped[datetime | None]
    registros: Mapped[int] = mapped_column(Integer, default=0)
    erro: Mapped[str | None]
