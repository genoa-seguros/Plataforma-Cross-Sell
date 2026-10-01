"""Integração com o Pipedrive.

Leitura (API v2, paginação por cursor): organizações, pessoas, etapas e negócios
de todos os funis configurados em config/verticais.yaml. Escrita: atividades
criadas pela plataforma, sempre vinculadas à pessoa (e ao negócio/organização).
"""

import re
from collections.abc import Iterator
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import Atividade, Negocio, Usuario
from crosssell.normalize import parse_data
from crosssell.resolver import resolver_empresa, resolver_pessoa

STATUS_MAP = {"open": "aberto", "won": "ganho", "lost": "perdido"}


class PipedriveClient:
    def __init__(self, token: str, domain: str = "api", transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=f"https://{domain}.pipedrive.com/api",
            params={"api_token": token},
            timeout=30,
            transport=transport,
        )

    def paginar(self, recurso: str, **params) -> Iterator[dict]:
        cursor = None
        while True:
            q = {"limit": 500, **params}
            if cursor:
                q["cursor"] = cursor
            r = self.http.get(f"/v2/{recurso}", params=q)
            r.raise_for_status()
            body = r.json()
            yield from body.get("data") or []
            cursor = (body.get("additional_data") or {}).get("next_cursor")
            if not cursor:
                break

    def usuarios(self) -> list[dict]:
        r = self.http.get("/v1/users")
        r.raise_for_status()
        return r.json().get("data") or []

    def criar_atividade(self, dados: dict) -> dict:
        r = self.http.post("/v2/activities", json=dados)
        r.raise_for_status()
        return r.json()["data"]

    def atualizar_atividade(self, atividade_id: int, dados: dict) -> dict:
        r = self.http.patch(f"/v2/activities/{atividade_id}", json=dados)
        r.raise_for_status()
        return r.json()["data"]

    def atividade(self, atividade_id: int) -> dict | None:
        r = self.http.get(f"/v2/activities/{atividade_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()["data"]


def cliente(settings: Settings) -> PipedriveClient:
    return PipedriveClient(settings.pipedrive_api_token, settings.pipedrive_company_domain)


def _primeiro(valores) -> str | None:
    if isinstance(valores, list):
        for v in valores:
            if isinstance(v, dict) and v.get("value"):
                return v["value"]
        return None
    return valores


def _rotulo(valor) -> str | None:
    """Rótulo de um campo de opção (com include_option_labels): {"label": ...} ou [{...}]."""
    if isinstance(valor, dict):
        return valor.get("label")
    if isinstance(valor, list):
        rotulos = [v.get("label") for v in valor if isinstance(v, dict) and v.get("label")]
        return ", ".join(rotulos) or None
    return None


def produto_do_negocio(d: dict, chaves_produto: list[str]) -> str | None:
    campos = d.get("custom_fields") or {}
    for chave in chaves_produto:
        rotulo = _rotulo(campos.get(chave))
        if rotulo:
            return rotulo
    # Sem campo de produto: usa o título sem o ano e complementos ("D&O 2026 Endosso" -> "D&O").
    titulo = (d.get("title") or "").strip()
    return re.split(r"\s+(?:19|20)\d{2}\b", titulo)[0].strip() or None


def vincular_usuarios(db: Session, client: PipedriveClient) -> dict[int, str]:
    """Liga os usuários da plataforma aos do Pipedrive pelo e-mail. Retorna id -> e-mail."""
    mapa = {}
    for u in client.usuarios():
        email = (u.get("email") or "").lower()
        mapa[u["id"]] = email
        local = db.scalar(select(Usuario).where(Usuario.email == email))
        if local is not None:
            local.pipedrive_user_id = u["id"]
    db.flush()
    return mapa


def sincronizar(db: Session, settings: Settings, client: PipedriveClient | None = None,
                updated_since: datetime | None = None) -> dict:
    client = client or cliente(settings)
    pipelines = settings.pipelines()
    campos_cfg = settings.campos_pipedrive()
    chaves_produto = campos_cfg.get("produto") or []
    filtro = {"updated_since": updated_since.strftime("%Y-%m-%dT%H:%M:%SZ")} if updated_since else {}
    try:
        donos = vincular_usuarios(db, client)
    except httpx.HTTPError:
        donos = {}
    etapas = {s["id"]: s.get("name") for s in client.paginar("stages")}

    contagem = {"organizacoes": 0, "pessoas": 0, "negocios": 0, "ignorados": 0}

    for org in client.paginar("organizations", **filtro):
        campos = org.get("custom_fields") or {}
        resolver_empresa(
            db,
            razao_social=org.get("name"),
            cnpj=campos.get(settings.pipedrive_cnpj_field),
            pipedrive_org_id=org["id"],
            website=org.get("website"),
            linkedin_url=org.get("linkedin"),
            setor=org.get("industry"),
            funcionarios=org.get("employee_count"),
        )
        contagem["organizacoes"] += 1

    for p in client.paginar("persons", **filtro):
        empresa = resolver_empresa(db, pipedrive_org_id=p.get("org_id"), criar=False) if p.get("org_id") else None
        resolver_pessoa(
            db,
            nome=p.get("name"),
            email=_primeiro(p.get("emails")),
            telefone=_primeiro(p.get("phones")),
            empresa=empresa,
            pipedrive_person_id=p["id"],
            cargo=p.get("job_title"),
            fonte="pipedrive",
        )
        contagem["pessoas"] += 1

    for d in client.paginar("deals", include_option_labels="true", **filtro):
        funil = pipelines.get(d.get("pipeline_id"))
        if funil is None:
            contagem["ignorados"] += 1
            continue
        empresa = resolver_empresa(db, pipedrive_org_id=d.get("org_id"), criar=False) if d.get("org_id") else None
        pessoa = resolver_pessoa(db, pipedrive_person_id=d.get("person_id"), criar=False) if d.get("person_id") else None
        neg = db.scalar(select(Negocio).where(Negocio.fonte == "pipedrive", Negocio.id_externo == str(d["id"])))
        if neg is None:
            neg = Negocio(fonte="pipedrive", id_externo=str(d["id"]), status="aberto")
            db.add(neg)
        campos = d.get("custom_fields") or {}
        neg.vertical = funil["vertical"]
        neg.pipeline_id = d.get("pipeline_id")
        neg.etapa = etapas.get(d.get("stage_id"))
        neg.titulo = d.get("title")
        neg.produto = produto_do_negocio(d, chaves_produto)
        neg.status = STATUS_MAP.get(d.get("status"), d.get("status") or "aberto")
        # Negócio "ganho" que na verdade registra um cancelamento (ex.: "Medmal 2026 Cancelamento").
        if neg.status == "ganho" and ("cancelamento" in (d.get("title") or "").lower() or (d.get("value") or 0) < 0):
            neg.status = "cancelado"
        neg.valor = d.get("value")
        neg.empresa = empresa
        neg.pessoa = pessoa
        neg.inicio_vigencia = parse_data(campos.get(campos_cfg.get("inicio_vigencia")))
        neg.fim_vigencia = parse_data(campos.get(campos_cfg.get("fim_vigencia")))
        neg.pipedrive_owner_id = d.get("owner_id")
        neg.responsavel_email = donos.get(d.get("owner_id"))
        # "Já possui o seguro saúde na Genoa?" = Sim -> registra a empresa como cliente Saúde.
        if empresa is not None and _rotulo(campos.get(campos_cfg.get("possui_saude"))) == "Sim":
            marcar_saude(db, empresa.id, True, origem="pipedrive")
        contagem["negocios"] += 1

    db.commit()
    return contagem


def marcar_saude(db: Session, empresa_id: int, cliente: bool, origem: str = "manual") -> Negocio:
    """Marca/desmarca a empresa como cliente Saúde fora da planilha do Zeca."""
    chave = f"saude-{empresa_id}"
    neg = db.scalar(select(Negocio).where(Negocio.fonte == "manual", Negocio.id_externo == chave))
    if neg is None:
        from crosssell.models import Empresa

        neg = Negocio(fonte="manual", id_externo=chave, empresa=db.get(Empresa, empresa_id), vertical="saude",
                      status="ativo")
        db.add(neg)
    neg.status = "ativo" if cliente else "cancelado"
    neg.titulo = neg.produto = "Saúde"
    neg.seguradora = "marcado na plataforma" if origem == "manual" else "informado no Pipedrive"
    db.flush()
    return neg


def criar_atividade(db: Session, client: PipedriveClient, *, negocio: Negocio, pessoa_id: int, assunto: str,
                    vencimento: date, responsavel: Usuario, criada_por: Usuario, tipo: str = "task",
                    nota: str | None = None) -> Atividade:
    from crosssell.models import Pessoa

    pessoa = db.get(Pessoa, pessoa_id)
    if pessoa is None or not pessoa.pipedrive_person_id:
        raise ValueError("A atividade precisa de uma pessoa que exista no Pipedrive.")
    if not responsavel.pipedrive_user_id:
        raise ValueError(f"{responsavel.nome} não está vinculado a um usuário do Pipedrive.")
    empresa = negocio.empresa
    dados = {
        "subject": assunto, "type": tipo, "due_date": vencimento.isoformat(),
        "owner_id": responsavel.pipedrive_user_id,
        "participants": [{"person_id": pessoa.pipedrive_person_id, "primary": True}],
        "note": nota or None,
    }
    if negocio.fonte == "pipedrive":
        dados["deal_id"] = int(negocio.id_externo)
    if empresa is not None and empresa.pipedrive_org_id:
        dados["org_id"] = empresa.pipedrive_org_id
    criada = client.criar_atividade({k: v for k, v in dados.items() if v is not None})
    at = Atividade(pipedrive_id=criada["id"], negocio_id=negocio.id, pessoa_id=pessoa.id,
                   empresa_id=negocio.empresa_id, assunto=assunto, tipo=tipo, vencimento=vencimento,
                   responsavel_id=responsavel.id, criada_por_id=criada_por.id, nota=nota)
    db.add(at)
    db.commit()
    return at


def concluir_atividade(db: Session, client: PipedriveClient, atividade: Atividade, concluida: bool = True) -> None:
    if atividade.pipedrive_id:
        client.atualizar_atividade(atividade.pipedrive_id, {"done": concluida})
    atividade.concluida = concluida
    atividade.concluida_em = datetime.utcnow() if concluida else None
    db.commit()


def sincronizar_atividades(db: Session, client: PipedriveClient, desde: date) -> int:
    """Atualiza no banco o status (feita/pendente) das atividades criadas pela plataforma."""
    n = 0
    for at in db.scalars(select(Atividade).where(Atividade.pipedrive_id.is_not(None), Atividade.vencimento >= desde)):
        dados = client.atividade(at.pipedrive_id)
        if dados is None:
            continue
        feita = bool(dados.get("done"))
        if feita != at.concluida:
            at.concluida = feita
            at.concluida_em = datetime.utcnow() if feita else None
            n += 1
    db.commit()
    return n
