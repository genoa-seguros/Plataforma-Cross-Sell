"""Modelo de dados unificado.

A chave de ligação entre as verticais é a Empresa (identificada por CNPJ,
com fallback para domínio de e-mail e nome normalizado) e a Pessoa
(identificada por e-mail). Tudo que é "produto" em qualquer vertical
— negócio no Pipedrive, apólice de saúde no Zeca ou marcação manual de
cliente Saúde — vira um registro de Negocio.
"""

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from crosssell.db import Base


def _now() -> datetime:
    return datetime.utcnow()


class Empresa(Base):
    __tablename__ = "empresas"

    id: Mapped[int] = mapped_column(primary_key=True)
    cnpj: Mapped[str | None] = mapped_column(String(14), index=True)  # repetido = organização duplicada
    razao_receita: Mapped[str | None]  # razão social oficial (Receita), para completar o nome no Pipedrive
    razao_social: Mapped[str]
    nome_fantasia: Mapped[str | None]
    nome_normalizado: Mapped[str] = mapped_column(index=True)
    dominio: Mapped[str | None] = mapped_column(index=True)
    website: Mapped[str | None]
    linkedin_url: Mapped[str | None]
    setor: Mapped[str | None]
    setor_fonte: Mapped[str | None]  # linkedin | manual (só esses valem como setor)
    cnae: Mapped[str | None]
    natureza_juridica: Mapped[str | None]  # Receita (ex.: "205-4 - Sociedade Anônima Fechada")
    descricao: Mapped[str | None]  # "Sobre" e especialidades da página do LinkedIn
    investida: Mapped[bool | None]  # LinkedIn: recebeu investimento de venture capital
    porte: Mapped[str | None]
    funcionarios: Mapped[int | None]
    funcionarios_fonte: Mapped[str | None]  # manual | linkedin | pipedrive | planilha
    funcionarios_em: Mapped[datetime | None]  # quando foi informado à mão (o LinkedIn só atualiza 180 dias depois)
    capital_social: Mapped[float | None]
    cidade: Mapped[str | None]
    uf: Mapped[str | None]
    cidade_fonte: Mapped[str | None]  # manual | receita | pipedrive | linkedin
    cidade_em: Mapped[datetime | None]  # quando foi informada à mão (vale sobre as outras fontes)
    # Praça de Saúde: "alvo" ou "fora" (cidade fora da lista e menos de 30% dos funcionários em cidades alvo)
    praca: Mapped[str | None]
    praca_fatia: Mapped[float | None]  # fatia dos funcionários (LinkedIn) em cidades alvo, 0 a 1
    praca_em: Mapped[datetime | None]  # quando a distribuição dos funcionários foi lida
    site_cnpj_em: Mapped[datetime | None]  # última procura do CNPJ no site da empresa
    dominios_extras: Mapped[list | None] = mapped_column(JSON)  # outros domínios de e-mail (site, LinkedIn, Receita)
    dominios_em: Mapped[datetime | None]  # última procura de domínios de e-mail
    pipedrive_org_id: Mapped[int | None] = mapped_column(index=True)
    enriquecido_em: Mapped[datetime | None]

    score_relacionamento: Mapped[float] = mapped_column(Float, default=0.0)
    score_componentes: Mapped[dict] = mapped_column(JSON, default=dict)

    noticias_em: Mapped[datetime | None]
    linkedin_em: Mapped[datetime | None]  # última leitura do LinkedIn (via n8n)
    linkedin_pedido_em: Mapped[datetime | None]  # último envio ao n8n
    linkedin_site_em: Mapped[datetime | None]  # última procura do link do LinkedIn no site da empresa
    linkedin_busca_em: Mapped[datetime | None]  # última busca do perfil na Linked API
    linkedin_nao_encontrado: Mapped[bool] = mapped_column(default=False)
    linkedin_areas: Mapped[dict] = mapped_column(JSON, default=dict)  # área -> data da última busca de funcionários

    pessoas: Mapped[list["Pessoa"]] = relationship(back_populates="empresa")
    negocios: Mapped[list["Negocio"]] = relationship(back_populates="empresa")
    noticias: Mapped[list["Noticia"]] = relationship(order_by="Noticia.publicada_em.desc()")


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
    # Temperatura dos e-mails que a pessoa escreve (IA): pouca | media | muita
    temperatura: Mapped[str | None]
    temperatura_motivo: Mapped[str | None]
    temperatura_em: Mapped[datetime | None]
    # LinkedIn (via n8n + Linked API)
    linkedin_headline: Mapped[str | None]
    linkedin_empresa_atual: Mapped[str | None]
    linkedin_em: Mapped[datetime | None]
    linkedin_pedido_em: Mapped[datetime | None]
    linkedin_busca_em: Mapped[datetime | None]
    linkedin_nao_encontrado: Mapped[bool] = mapped_column(default=False)

    empresa: Mapped[Empresa | None] = relationship(back_populates="pessoas")
    negocios: Mapped[list["Negocio"]] = relationship(back_populates="pessoa")


class Usuario(Base):
    """Pessoa da Innoa com acesso à plataforma. A caixa de e-mail só é lida se o master ligar a leitura
    (tela Equipe) e a pessoa estiver no grupo da Access Policy do Microsoft 365."""

    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
    nome: Mapped[str]
    papel: Mapped[str] = mapped_column(default="membro")  # master | head (também acessa a Qualidade) | membro
    ativo: Mapped[bool] = mapped_column(default=True)
    senha_hash: Mapped[str | None]
    convite_token: Mapped[str | None] = mapped_column(unique=True)
    convite_expira: Mapped[datetime | None]
    redefinir_token: Mapped[str | None] = mapped_column(unique=True)  # "esqueci minha senha" (hash)
    redefinir_expira: Mapped[datetime | None]
    verticais: Mapped[list] = mapped_column(JSON, default=list)
    lider: Mapped[list] = mapped_column(JSON, default=list)
    pipedrive_user_id: Mapped[int | None]
    criado_em: Mapped[datetime] = mapped_column(default=_now)
    # Leitura da caixa de e-mail: desligada até o master ligar (e só vale para quem já entrou)
    le_emails: Mapped[bool] = mapped_column(default=False, server_default=false())
    leitura_em: Mapped[datetime | None]  # última leitura da caixa que deu certo
    leitura_erro: Mapped[str | None]  # erro da última tentativa ("recusada": fora do grupo no Microsoft 365)
    historico_em: Mapped[datetime | None]  # quando a caixa teve os últimos 12 meses lidos (uma vez, ao ligar)

    @property
    def pendente(self) -> bool:
        """Convidado, mas ainda não criou a senha (nunca entrou)."""
        return self.senha_hash is None


class Sessao(Base):
    __tablename__ = "sessoes"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    expira: Mapped[datetime]

    usuario: Mapped[Usuario] = relationship()


class Negocio(Base):
    """Um produto/relacionamento comercial em uma vertical."""

    __tablename__ = "negocios"
    __table_args__ = (UniqueConstraint("fonte", "id_externo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int | None] = mapped_column(ForeignKey("empresas.id"), index=True)
    pessoa_id: Mapped[int | None] = mapped_column(ForeignKey("pessoas.id"), index=True)
    vertical: Mapped[str | None] = mapped_column(index=True)  # None: funil sem vertical (Canais Parceria)
    fonte: Mapped[str]  # pipedrive | zeca | manual
    id_externo: Mapped[str]
    titulo: Mapped[str | None]
    pipeline_id: Mapped[int | None] = mapped_column(index=True)
    etapa: Mapped[str | None]
    pipedrive_owner_id: Mapped[int | None]
    # aberto | ganho | perdido (pipeline) ; ativo | cancelado (apólices)
    status: Mapped[str] = mapped_column(index=True)
    produto: Mapped[str | None]
    seguradora: Mapped[str | None]
    inicio_vigencia: Mapped[date | None] = mapped_column(Date)
    fim_vigencia: Mapped[date | None] = mapped_column(Date)
    ganho_em: Mapped[date | None] = mapped_column(Date)  # Pipedrive won_time
    valor: Mapped[float | None]
    vidas: Mapped[int | None]
    responsavel_email: Mapped[str | None]
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    empresa: Mapped[Empresa | None] = relationship(back_populates="negocios")
    pessoa: Mapped[Pessoa | None] = relationship(back_populates="negocios")

    @property
    def vigente(self) -> bool:
        """Seguro vigente: negócio GANHO com fim de vigência depois de hoje.

        Negócio aberto no funil não torna a empresa cliente. Apólices do Zeca
        e marcações manuais de Saúde valem enquanto ativas (e dentro do fim, se houver).
        """
        hoje = date.today()
        if self.saude_vitalicio:
            return not self.saude_desmarcada
        if self.fonte == "pipedrive":
            return self.status == "ganho" and self.fim_vigencia is not None and self.fim_vigencia > hoje
        if self.status != "ativo":
            return False
        return self.fim_vigencia is None or self.fim_vigencia >= hoje

    @property
    def saude_vitalicio(self) -> bool:
        """Saúde ganho no Pipedrive sem fim de vigência: o contrato vale até o cliente cancelar."""
        return (self.fonte == "pipedrive" and self.vertical == "saude" and self.status == "ganho"
                and self.fim_vigencia is None)

    @property
    def saude_desmarcada(self) -> bool:
        """Alguém informou na plataforma que a empresa não é mais cliente Saúde."""
        return any(n.fonte == "manual" and n.vertical == "saude" and n.status == "cancelado"
                   for n in (self.empresa.negocios if self.empresa else []))

    @property
    def ex_cliente(self) -> bool:
        """Já foi cliente nesta vertical (vigência vencida ou apólice cancelada)."""
        if self.saude_vitalicio:
            return self.saude_desmarcada
        return self.status == "cancelado" or (self.status in ("ganho", "ativo") and not self.vigente
                                              and self.fim_vigencia is not None)


class Interacao(Base):
    """Metadado de uma troca de e-mail entre um usuário interno e um contato externo.

    Guardamos apenas metadados (quem, quando, direção, thread). O texto dos
    e-mails recebidos é lido só para medir a temperatura e não é armazenado.
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


class Atividade(Base):
    """Atividade criada pela plataforma no Pipedrive (dentro da pessoa)."""

    __tablename__ = "atividades"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipedrive_id: Mapped[int | None] = mapped_column(unique=True)
    negocio_id: Mapped[int | None] = mapped_column(ForeignKey("negocios.id"), index=True)
    pessoa_id: Mapped[int | None] = mapped_column(ForeignKey("pessoas.id"), index=True)
    empresa_id: Mapped[int | None] = mapped_column(ForeignKey("empresas.id"), index=True)
    assunto: Mapped[str]
    tipo: Mapped[str] = mapped_column(default="task")
    vencimento: Mapped[date] = mapped_column(Date, index=True)
    responsavel_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    criada_por_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    nota: Mapped[str | None]
    concluida: Mapped[bool] = mapped_column(default=False)
    concluida_em: Mapped[datetime | None]
    criada_em: Mapped[datetime] = mapped_column(default=_now)

    negocio: Mapped["Negocio | None"] = relationship()
    pessoa: Mapped[Pessoa | None] = relationship()
    empresa: Mapped[Empresa | None] = relationship()
    responsavel: Mapped[Usuario] = relationship(foreign_keys=[responsavel_id])


class Noticia(Base):
    __tablename__ = "noticias"
    __table_args__ = (UniqueConstraint("empresa_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    titulo: Mapped[str]
    fonte: Mapped[str | None]
    url: Mapped[str]
    publicada_em: Mapped[datetime | None] = mapped_column(index=True)
    coletada_em: Mapped[datetime] = mapped_column(default=_now)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    fonte: Mapped[str]
    inicio: Mapped[datetime] = mapped_column(default=_now)
    fim: Mapped[datetime | None]
    registros: Mapped[int] = mapped_column(Integer, default=0)
    erro: Mapped[str | None]


class LinkedinPedido(Base):
    """Pedido feito à Linked API e ainda não aplicado (a resposta é assíncrona)."""

    __tablename__ = "linkedin_pedidos"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[str] = mapped_column(unique=True)
    id_alvo: Mapped[str] = mapped_column(index=True)  # P123 | E45
    acao: Mapped[str]  # ler | buscar | area | praca | pessoas
    area: Mapped[str | None]
    vertical: Mapped[str | None]  # vertical que pagou a consulta (cota do dia)
    grupo: Mapped[str | None]  # caracteristicas | pessoas | geral
    criado_em: Mapped[datetime] = mapped_column(default=_now, index=True)
    concluido_em: Mapped[datetime | None] = mapped_column(index=True)
    situacao: Mapped[str] = mapped_column(default="pendente")  # pendente | aplicado | erro | expirado
    erro: Mapped[str | None]


class QualidadeIgnorada(Base):
    """Sugestão da aba Qualidade que o master marcou como "não é problema" (ex.: matriz e filial)."""

    __tablename__ = "qualidade_ignoradas"

    id: Mapped[int] = mapped_column(primary_key=True)
    chave: Mapped[str] = mapped_column(unique=True)  # dup:<ids> | razao:<empresa_id>
    em: Mapped[datetime] = mapped_column(default=_now)


class Melhoria(Base):
    """Ideia de melhoria anotada na plataforma (ex.: durante a reunião de sexta)."""

    __tablename__ = "melhorias"

    id: Mapped[int] = mapped_column(primary_key=True)
    titulo: Mapped[str]
    descricao: Mapped[str | None]
    prioridade: Mapped[str] = mapped_column(default="media")  # alta | media | baixa
    situacao: Mapped[str] = mapped_column(default="nova", index=True)  # nova | andamento | feita | descartada
    autor_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    criada_em: Mapped[datetime] = mapped_column(default=_now)
    atualizada_em: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    autor: Mapped[Usuario] = relationship()


class Configuracao(Base):
    """Configuração alterada pelo master na plataforma (ex.: modelo da IA). Vale sobre o .env."""

    __tablename__ = "configuracoes"

    chave: Mapped[str] = mapped_column(primary_key=True)
    valor: Mapped[str]
    atualizado_em: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
    atualizado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"))
