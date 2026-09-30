"""Sincronização com o Pipedrive (Linhas Financeiras, RE e novos negócios de Saúde).

Usa a API v2 (organizations, persons, deals) com paginação por cursor.
A vertical de cada negócio vem do pipeline, conforme config/verticais.yaml.
"""

from collections.abc import Iterator
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import Negocio
from crosssell.normalize import parse_data
from crosssell.resolver import resolver_empresa, resolver_pessoa

STATUS_MAP = {"open": "aberto", "won": "ganho", "lost": "perdido"}


class PipedriveClient:
    def __init__(self, token: str, domain: str = "api", transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=f"https://{domain}.pipedrive.com/api/v2",
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
            r = self.http.get(f"/{recurso}", params=q)
            r.raise_for_status()
            body = r.json()
            yield from body.get("data") or []
            cursor = (body.get("additional_data") or {}).get("next_cursor")
            if not cursor:
                break

    def usuarios(self) -> dict[int, str]:
        """id -> e-mail dos usuários do Pipedrive (endpoint v1)."""
        r = self.http.get(str(self.http.base_url).replace("/v2", "/v1") + "/users")
        r.raise_for_status()
        return {u["id"]: u.get("email", "").lower() for u in r.json().get("data") or []}


def _primeiro(valores) -> str | None:
    if isinstance(valores, list):
        for v in valores:
            if isinstance(v, dict) and v.get("value"):
                return v["value"]
        return None
    return valores


def sincronizar(db: Session, settings: Settings, client: PipedriveClient | None = None,
                updated_since: datetime | None = None) -> dict:
    client = client or PipedriveClient(settings.pipedrive_api_token, settings.pipedrive_company_domain)
    pipelines = {int(k): v for k, v in (settings.verticais_config().get("pipedrive", {}).get("pipelines") or {}).items()}
    filtro = {"updated_since": updated_since.strftime("%Y-%m-%dT%H:%M:%SZ")} if updated_since else {}
    try:
        donos = client.usuarios()
    except httpx.HTTPError:
        donos = {}

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

    for d in client.paginar("deals", **filtro):
        vertical = pipelines.get(d.get("pipeline_id"))
        if not vertical:
            contagem["ignorados"] += 1
            continue
        empresa = resolver_empresa(db, pipedrive_org_id=d.get("org_id"), criar=False) if d.get("org_id") else None
        pessoa = resolver_pessoa(db, pipedrive_person_id=d.get("person_id"), criar=False) if d.get("person_id") else None
        neg = db.scalar(select(Negocio).where(Negocio.fonte == "pipedrive", Negocio.id_externo == str(d["id"])))
        if neg is None:
            neg = Negocio(fonte="pipedrive", id_externo=str(d["id"]), vertical=vertical, status="aberto")
            db.add(neg)
        neg.vertical = vertical
        neg.titulo = d.get("title")
        neg.status = STATUS_MAP.get(d.get("status"), d.get("status") or "aberto")
        neg.valor = d.get("value")
        neg.empresa_id = empresa.id if empresa else None
        neg.pessoa_id = pessoa.id if pessoa else None
        neg.inicio_vigencia = parse_data(d.get("won_time")) or neg.inicio_vigencia
        neg.fim_vigencia = parse_data(d.get("expected_close_date")) if neg.status == "aberto" else neg.fim_vigencia
        neg.responsavel_email = donos.get(d.get("owner_id"))
        contagem["negocios"] += 1

    db.commit()
    return contagem
