"""Leitura de metadados de e-mail dos usuários das verticais via Microsoft Graph.

Requer um app registration no Entra ID com permissão de *aplicativo* Mail.Read
(com consentimento de admin). Recomenda-se restringir o app às caixas dos
usuários das verticais com uma Application Access Policy do Exchange.

Somente metadados são lidos ($select sem subject/body): remetente,
destinatários, data e conversationId. Isso basta para medir frequência,
recência, reciprocidade e amplitude do relacionamento.
"""

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import Empresa, Interacao, Pessoa
from crosssell.normalize import dominio_email, normalizar_email
from crosssell.resolver import resolver_pessoa

GRAPH = "https://graph.microsoft.com/v1.0"
CAMPOS = "id,conversationId,from,toRecipients,ccRecipients,sentDateTime,receivedDateTime"


class GraphClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.http = httpx.Client(timeout=60, transport=transport)
        self._token: str | None = None

    def token(self) -> str:
        if self._token is None:
            r = self.http.post(
                f"https://login.microsoftonline.com/{self.settings.ms_tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": self.settings.ms_client_id,
                    "client_secret": self.settings.ms_client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
            )
            r.raise_for_status()
            self._token = r.json()["access_token"]
        return self._token

    def mensagens(self, usuario: str, desde: datetime) -> Iterator[dict]:
        url = f"{GRAPH}/users/{usuario}/messages"
        params = {
            "$select": CAMPOS,
            "$filter": f"receivedDateTime ge {desde.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$top": 200,
        }
        while url:
            r = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self.token()}"})
            r.raise_for_status()
            body = r.json()
            yield from body.get("value", [])
            url, params = body.get("@odata.nextLink"), None


def _enderecos(lista) -> list[str]:
    return [e for e in (normalizar_email((x.get("emailAddress") or {}).get("address")) for x in lista or []) if e]


def converter_graph(msg: dict) -> dict:
    """Converte uma mensagem do Graph para o formato neutro usado por registrar_mensagens."""
    return {
        "message_id": msg["id"],
        "thread_id": msg.get("conversationId"),
        "data": msg.get("sentDateTime") or msg.get("receivedDateTime"),
        "de": (_enderecos([msg.get("from")]) or [None])[0],
        "para": _enderecos(msg.get("toRecipients")) + _enderecos(msg.get("ccRecipients")),
    }


def registrar_mensagens(db: Session, settings: Settings, usuario_email: str, mensagens: Iterable[dict]) -> dict:
    """Grava uma Interacao por (mensagem, participante externo).

    Participantes de domínios de empresas já conhecidas viram Pessoa
    automaticamente — assim descobrimos pontos focais que não estão no CRM.
    """
    internos = settings.internal_domain_set
    usuario_email = usuario_email.lower()
    cont = {"mensagens": 0, "interacoes": 0, "novos_contatos": 0}
    dominios_empresa = {e.dominio: e for e in db.scalars(select(Empresa).where(Empresa.dominio.is_not(None)))}

    for m in mensagens:
        cont["mensagens"] += 1
        de = m.get("de")
        enviado = de == usuario_email
        participantes = m.get("para", []) if enviado else [de]
        data = m["data"]
        if isinstance(data, str):
            data = datetime.fromisoformat(data.replace("Z", "+00:00")).replace(tzinfo=None)

        for ext in {p for p in participantes if p}:
            dom = ext.split("@", 1)[1]
            if dom in internos:
                continue
            if db.scalar(select(Interacao.id).where(Interacao.message_id == m["message_id"],
                                                    Interacao.email_externo == ext)):
                continue
            pessoa = db.scalar(select(Pessoa).where(Pessoa.email == ext))
            empresa = dominios_empresa.get(dominio_email(ext) or "")
            if pessoa is None and empresa is not None:
                pessoa = resolver_pessoa(db, email=ext, empresa=empresa, fonte="email")
                cont["novos_contatos"] += 1
            db.add(Interacao(
                message_id=m["message_id"],
                thread_id=m.get("thread_id"),
                data=data,
                usuario_email=usuario_email,
                email_externo=ext,
                direcao="enviado" if enviado else "recebido",
                pessoa_id=pessoa.id if pessoa else None,
                empresa_id=(pessoa.empresa_id if pessoa and pessoa.empresa_id else (empresa.id if empresa else None)),
            ))
            cont["interacoes"] += 1
    db.commit()
    return cont


def sincronizar(db: Session, settings: Settings, dias: int = 30, client: GraphClient | None = None) -> dict:
    client = client or GraphClient(settings)
    desde = datetime.utcnow() - timedelta(days=dias)
    usuarios = [u["email"] for u in settings.usuarios()]
    total: dict[str, int] = {}
    for u in usuarios:
        res = registrar_mensagens(db, settings, u, (converter_graph(m) for m in client.mensagens(u, desde)))
        for k, v in res.items():
            total[k] = total.get(k, 0) + v
    return total
