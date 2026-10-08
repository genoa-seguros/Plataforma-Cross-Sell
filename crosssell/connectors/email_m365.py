"""Leitura de metadados de e-mail dos usuários das verticais via Microsoft Graph.

Requer um app registration no Entra ID com permissão de *aplicativo* Mail.Read
(com consentimento de admin). Recomenda-se restringir o app às caixas dos
usuários das verticais com uma Application Access Policy do Exchange.

Ficam gravados só os metadados (remetente, destinatários, data e
conversationId), que medem frequência, recência, reciprocidade e amplitude
do relacionamento. Dos e-mails RECEBIDOS de contatos conhecidos lemos também
o trecho novo da resposta (uniqueBody, sem o histórico citado). Esse texto
vai para a classificação de temperatura e é descartado em seguida.

As caixas lidas são as dos usuários que já entraram na plataforma e com a leitura ligada pelo master
(tela Equipe). O Microsoft 365 ainda restringe o app ao grupo da Access Policy: fora dele, a leitura
é recusada (403) e a tela Equipe mostra "recusada".
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
CAMPOS = "id,conversationId,isDraft,from,toRecipients,ccRecipients,sentDateTime,receivedDateTime,uniqueBody"


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

    def enviar(self, remetente: str, para: str, assunto: str, html: str) -> None:
        """Envia um e-mail pela caixa `remetente` (permissão de aplicativo Mail.Send)."""
        r = self.http.post(f"{GRAPH}/users/{remetente}/sendMail", headers={"Authorization": f"Bearer {self.token()}"},
                           json={"message": {"subject": assunto, "body": {"contentType": "HTML", "content": html},
                                             "toRecipients": [{"emailAddress": {"address": para}}]},
                                 "saveToSentItems": False})
        r.raise_for_status()

    def mensagens(self, usuario: str, desde: datetime) -> Iterator[dict]:
        url = f"{GRAPH}/users/{usuario}/messages"
        params = {
            "$select": CAMPOS,
            "$filter": f"receivedDateTime ge {desde.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$top": 200,
        }
        while url:
            r = self.http.get(url, params=params, headers={
                "Authorization": f"Bearer {self.token()}",
                "Prefer": 'outlook.body-content-type="text"',
            })
            r.raise_for_status()
            body = r.json()
            # Rascunho não foi trocado com ninguém (e vem sem remetente): não conta como interação
            yield from (m for m in body.get("value", []) if not m.get("isDraft"))
            url, params = body.get("@odata.nextLink"), None


def _enderecos(lista) -> list[str]:
    return [e for e in (normalizar_email(((x or {}).get("emailAddress") or {}).get("address")) for x in lista or []) if e]


def converter_graph(msg: dict) -> dict:
    """Converte uma mensagem do Graph para o formato neutro usado por registrar_mensagens."""
    return {
        "message_id": msg["id"],
        "thread_id": msg.get("conversationId"),
        "data": msg.get("sentDateTime") or msg.get("receivedDateTime"),
        "de": (_enderecos([msg.get("from")]) or [None])[0],
        "para": _enderecos(msg.get("toRecipients")) + _enderecos(msg.get("ccRecipients")),
        "texto": (msg.get("uniqueBody") or {}).get("content") or "",
    }


def dominios_empresas(db: Session, internos: set[str]) -> dict[str, Empresa]:
    """Domínio de e-mail -> empresa. Além do domínio do site, valem os domínios achados no site, no LinkedIn e na
    Receita (Empresa.dominios_extras) e o domínio corporativo dos contatos da empresa (Pipedrive, planilha, à mão): a Pro-Eficiência tem site intergado.com.br e e-mails @pontaagro.com.
    Um domínio usado por contatos de mais de uma empresa fica de fora (não dá para saber de qual é)."""
    mapa = {e.dominio: e for e in db.scalars(select(Empresa).where(Empresa.dominio.is_not(None)))}
    donos: dict[str, set[int]] = {}
    # Domínios achados no site, no LinkedIn e na Receita (Empresa.dominios_extras)
    for e in db.scalars(select(Empresa).where(Empresa.dominios_extras.is_not(None))):
        for dom in e.dominios_extras or []:
            if dom not in internos and dom not in mapa:
                donos.setdefault(dom, set()).add(e.id)
    for email, empresa_id in db.execute(select(Pessoa.email, Pessoa.empresa_id).where(
            Pessoa.email.is_not(None), Pessoa.empresa_id.is_not(None), Pessoa.fonte != "email")):
        dom = dominio_email(email)
        if dom and dom not in internos and dom not in mapa:
            donos.setdefault(dom, set()).add(empresa_id)
    unicos = {dom: ids.pop() for dom, ids in donos.items() if len(ids) == 1}
    empresas = {e.id: e for e in db.scalars(select(Empresa).where(Empresa.id.in_(set(unicos.values()))))}
    mapa.update({dom: empresas[eid] for dom, eid in unicos.items() if eid in empresas})
    return mapa


def religar_interacoes(db: Session, settings: Settings) -> int:
    """E-mails já lidos de quem ainda não tinha empresa (domínio desconhecido na época) passam para a empresa
    quando o domínio passa a ser conhecido; o contato é criado como na leitura normal."""
    mapa = dominios_empresas(db, settings.internal_domain_set)
    religadas = 0
    for i in db.scalars(select(Interacao).where(Interacao.empresa_id.is_(None))).all():
        empresa = mapa.get(dominio_email(i.email_externo) or "")
        if empresa is None:
            continue
        pessoa = db.scalar(select(Pessoa).where(Pessoa.email == i.email_externo)) or \
            resolver_pessoa(db, email=i.email_externo, empresa=empresa, fonte="email")
        if pessoa.empresa_id is None:
            pessoa.empresa_id = empresa.id
        i.pessoa_id, i.empresa_id = pessoa.id, pessoa.empresa_id
        religadas += 1
    db.commit()
    return religadas


def registrar_mensagens(db: Session, settings: Settings, usuario_email: str, mensagens: Iterable[dict],
                        textos: dict[int, list[dict]] | None = None) -> dict:
    """Grava uma Interacao por (mensagem, participante externo).

    Participantes de domínios de empresas já conhecidas viram Pessoa
    automaticamente — assim descobrimos pontos focais que não estão no CRM.
    Se `textos` for passado, recebe {pessoa_id: [{data, texto}]} dos e-mails
    novos que cada contato escreveu (para a temperatura; não é gravado).
    """
    internos = settings.internal_domain_set
    usuario_email = usuario_email.lower()
    cont = {"mensagens": 0, "interacoes": 0, "novos_contatos": 0}
    dominios_empresa = dominios_empresas(db, internos)

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
            if textos is not None and not enviado and pessoa is not None and m.get("texto"):
                textos.setdefault(pessoa.id, []).append({"data": data, "texto": m["texto"]})
    db.commit()
    return cont


def sincronizar(db: Session, settings: Settings, dias: int = 30, client: GraphClient | None = None,
                classificador=None) -> dict:
    from crosssell.models import Usuario
    from crosssell import temperatura

    client = client or GraphClient(settings)
    desde = datetime.utcnow() - timedelta(days=dias)
    usuarios = db.scalars(select(Usuario).where(Usuario.ativo.is_(True), Usuario.le_emails.is_(True),
                                                Usuario.senha_hash.is_not(None))).all()
    total: dict[str, int] = {}
    textos: dict[int, list[dict]] = {}
    falhas = []
    for u in usuarios:
        parcial: dict[int, list[dict]] = {}
        try:
            res = registrar_mensagens(db, settings, u.email, (converter_graph(m) for m in client.mensagens(u.email, desde)),
                                      parcial)
        except httpx.HTTPError as exc:  # ex.: caixa fora da Access Policy (403): as outras seguem
            db.rollback()  # descarta o que esta caixa deixou pela metade
            recusada = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403
            u.leitura_erro = "recusada" if recusada else (str(exc) or type(exc).__name__)[:300]
            db.commit()
            falhas.append(f"{u.email} ({exc})")
            continue
        u.leitura_em, u.leitura_erro = datetime.utcnow(), None
        db.commit()
        for pessoa_id, lista in parcial.items():
            textos.setdefault(pessoa_id, []).extend(lista)
        for k, v in res.items():
            total[k] = total.get(k, 0) + v
    if usuarios:
        total["religadas"] = religar_interacoes(db, settings)
    if classificador is not None and textos:
        total["temperaturas"] = temperatura.atualizar(db, classificador, textos)
    if falhas:  # o que foi lido já está gravado; o erro fica no log da sincronização
        raise RuntimeError(f"{len(falhas)} de {len(usuarios)} caixas não foram lidas: " + "; ".join(falhas))
    return total
