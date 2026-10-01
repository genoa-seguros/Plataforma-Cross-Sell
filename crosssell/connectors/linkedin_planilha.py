"""LinkedIn via Google Sheets + n8n (Linked API).

Fluxo:
  1. A plataforma escreve a aba "Alvos": pessoas e empresas dos negócios abertos
     que ainda não foram lidas no LinkedIn (ou cuja leitura passou da validade).
  2. Um fluxo no n8n lê "Alvos", chama a Linked API (fetchPerson / fetchCompany)
     e grava uma linha por alvo na aba "Resultados" (colunas em RESULTADOS).
  3. A plataforma lê "Resultados" e atualiza pessoas, empresas, decisores e posts.

O perfil só é aceito se o nome bater com o da plataforma (evita gravar um homônimo
quando o n8n precisou buscar o perfil pelo nome).
"""

import json
import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import Settings
from crosssell.models import Empresa, Noticia, Pessoa
from crosssell.normalize import classificar_senioridade, dominio_site, normalizar_nome_empresa, normalizar_nome_pessoa
from crosssell.resolver import resolver_pessoa

SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
ESCOPO = "https://www.googleapis.com/auth/spreadsheets"

ALVOS = ["id_alvo", "tipo", "nome", "empresa", "cargo", "email", "linkedin_url", "site", "cnpj", "motivo", "pedido_em"]
RESULTADOS = ["id_alvo", "tipo", "linkedin_url", "nome", "headline", "cargo_atual", "empresa_atual", "localizacao",
              "setor", "funcionarios", "site", "sede", "decisores", "posts", "capturado_em", "erro"]


class SheetsClient:
    def __init__(self, spreadsheet_id: str, token: callable, transport: httpx.BaseTransport | None = None):
        self.id = spreadsheet_id
        self.token = token
        self.http = httpx.Client(timeout=30, transport=transport)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    def ler(self, aba: str) -> list[list[str]]:
        r = self.http.get(f"{SHEETS}/{self.id}/values/{aba}!A:Z", headers=self._h())
        r.raise_for_status()
        return r.json().get("values", [])

    def substituir(self, aba: str, linhas: list[list]) -> None:
        self.http.post(f"{SHEETS}/{self.id}/values/{aba}!A:Z:clear", headers=self._h()).raise_for_status()
        r = self.http.put(f"{SHEETS}/{self.id}/values/{aba}!A1", params={"valueInputOption": "RAW"},
                          headers=self._h(), json={"values": linhas})
        r.raise_for_status()


def cliente(settings: Settings) -> SheetsClient:
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials

    cred = Credentials.from_service_account_file(settings.google_service_account_file, scopes=[ESCOPO])

    def token() -> str:
        if not cred.valid:
            cred.refresh(Request())
        return cred.token

    return SheetsClient(settings.google_sheets_id, token)


# --- 1. Alvos --------------------------------------------------------------

def _vencido(quando: datetime | None, dias: int) -> bool:
    return quando is None or quando < datetime.utcnow() - timedelta(days=dias)


def alvos(db: Session, settings: Settings) -> list[dict]:
    from crosssell import tabela

    validade = settings.linkedin_validade_dias
    empresas: dict[int, str] = {}
    pessoas: dict[int, str] = {}
    for linha in tabela.montar(db, settings):
        if linha["empresa"]:
            empresas.setdefault(linha["empresa"]["id"], f"{linha['funil']}: {linha['titulo']}")
        if linha["pessoa"]:
            pessoas.setdefault(linha["pessoa"]["id"], "contato do negócio")
        for c in linha["contatos"]:
            pessoas.setdefault(c["id"], "contato da empresa")

    saida = []
    agora = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    for e in db.scalars(select(Empresa).where(Empresa.id.in_(empresas))):
        if _vencido(e.linkedin_em, validade):
            saida.append({"id_alvo": f"E{e.id}", "tipo": "empresa", "nome": e.nome_fantasia or e.razao_social,
                          "empresa": e.razao_social, "cargo": "", "email": "", "linkedin_url": e.linkedin_url or "",
                          "site": e.website or (f"https://{e.dominio}" if e.dominio else ""), "cnpj": e.cnpj or "",
                          "motivo": empresas[e.id], "pedido_em": agora})
    for p in db.scalars(select(Pessoa).where(Pessoa.id.in_(pessoas))):
        if _vencido(p.linkedin_em, validade):
            saida.append({"id_alvo": f"P{p.id}", "tipo": "pessoa", "nome": p.nome,
                          "empresa": p.empresa.razao_social if p.empresa else "", "cargo": p.cargo or "",
                          "email": p.email or "", "linkedin_url": p.linkedin_url or "", "site": "", "cnpj": "",
                          "motivo": pessoas[p.id], "pedido_em": agora})
    return saida


def exportar(db: Session, settings: Settings, sheets: SheetsClient) -> dict:
    lista = alvos(db, settings)
    sheets.substituir(settings.linkedin_aba_alvos, [ALVOS] + [[a[c] for c in ALVOS] for a in lista])
    return {"alvos": len(lista), "empresas": sum(a["tipo"] == "empresa" for a in lista),
            "pessoas": sum(a["tipo"] == "pessoa" for a in lista)}


# --- 4. Resultados ---------------------------------------------------------

def _data(valor: str) -> datetime:
    v = (valor or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v[:19].replace("Z", ""), fmt)
        except ValueError:
            continue
    return datetime.utcnow()


def _lista(valor: str) -> list[dict]:
    """Aceita JSON (lista de objetos) ou linhas "Nome | Cargo | URL"."""
    v = (valor or "").strip()
    if not v:
        return []
    try:
        dados = json.loads(v)
        return [d for d in dados if isinstance(d, dict)] if isinstance(dados, list) else []
    except json.JSONDecodeError:
        itens = []
        for linha in v.splitlines():
            partes = [x.strip() for x in linha.split("|")]
            if partes and partes[0]:
                itens.append({"nome": partes[0], "headline": partes[1] if len(partes) > 1 else "",
                              "linkedin_url": partes[2] if len(partes) > 2 else ""})
        return itens


def _num(valor) -> int | None:
    """'1.200', '201-500' ou 350 -> número (no intervalo, o limite superior)."""
    digitos = re.findall(r"\d[\d.]*", str(valor or ""))
    return int(digitos[-1].replace(".", "")) if digitos else None


def mesmo_nome(a: str, b: str) -> bool:
    ta, tb = normalizar_nome_pessoa(a).split(), set(normalizar_nome_pessoa(b).split())
    return bool(ta) and ta[0] in tb and ta[-1] in tb


def _aplicar_pessoa(db: Session, p: Pessoa, r: dict, quando: datetime) -> str:
    if r.get("nome") and not mesmo_nome(p.nome, r["nome"]):
        return "nao_confirmado"
    p.linkedin_url = r.get("linkedin_url") or p.linkedin_url
    p.linkedin_headline = r.get("headline") or p.linkedin_headline
    p.linkedin_empresa_atual = r.get("empresa_atual") or p.linkedin_empresa_atual
    if r.get("cargo_atual") and not p.cargo:
        p.cargo = r["cargo_atual"]
    if p.cargo or p.linkedin_headline:
        p.senioridade = classificar_senioridade(p.cargo or p.linkedin_headline)
    p.linkedin_em = quando
    return "pessoas"


def _aplicar_empresa(db: Session, e: Empresa, r: dict, quando: datetime) -> tuple[str, int]:
    e.linkedin_url = r.get("linkedin_url") or e.linkedin_url
    e.setor = r.get("setor") or e.setor
    e.funcionarios = _num(r.get("funcionarios")) or e.funcionarios
    if r.get("site"):
        e.website = e.website or r["site"]
        e.dominio = e.dominio or dominio_site(r["site"])
    if r.get("sede") and not e.cidade:
        e.cidade = r["sede"]
    novos = 0
    for d in _lista(r.get("decisores"))[:20]:
        nome = d.get("nome") or d.get("name")
        if not nome:
            continue
        headline = d.get("headline") or d.get("cargo") or ""
        url = d.get("linkedin_url") or d.get("url") or None
        ja = db.scalar(select(Pessoa).where(Pessoa.empresa_id == e.id,
                                            Pessoa.nome_normalizado == normalizar_nome_pessoa(nome)))
        p = ja or resolver_pessoa(db, nome=nome, empresa=e, cargo=headline or None, linkedin_url=url, fonte="linkedin")
        p.linkedin_url = p.linkedin_url or url
        p.linkedin_headline = headline or p.linkedin_headline
        p.senioridade = p.senioridade or classificar_senioridade(headline)
        p.linkedin_em = quando
        novos += ja is None
    for post in _lista(r.get("posts"))[:5]:
        url, texto = post.get("url"), (post.get("texto") or post.get("text") or "").strip()
        if not url or not texto or db.scalar(select(Noticia.id).where(Noticia.empresa_id == e.id, Noticia.url == url)):
            continue
        titulo = texto.splitlines()[0][:180]
        db.add(Noticia(empresa_id=e.id, titulo=titulo, fonte="Post no LinkedIn", url=url,
                       publicada_em=_data(post.get("data") or post.get("date") or "")))
    e.linkedin_em = quando
    return "empresas", novos


def importar(db: Session, settings: Settings, sheets: SheetsClient) -> dict:
    valores = sheets.ler(settings.linkedin_aba_resultados)
    cont = {"linhas": 0, "pessoas": 0, "empresas": 0, "decisores_novos": 0, "nao_confirmado": 0, "erros": 0,
            "ja_lidos": 0}
    if not valores:
        return cont
    cab = [c.strip().lower() for c in valores[0]]
    for bruta in valores[1:]:
        r = {c: (bruta[i].strip() if i < len(bruta) else "") for i, c in enumerate(cab)}
        cont["linhas"] += 1
        if r.get("erro"):
            cont["erros"] += 1
            continue
        alvo = r.get("id_alvo", "")
        quando = _data(r.get("capturado_em", ""))
        obj = db.get(Pessoa if alvo.startswith("P") else Empresa, int(alvo[1:])) if alvo[1:].isdigit() else None
        if obj is None:
            cont["erros"] += 1
            continue
        if obj.linkedin_em and obj.linkedin_em >= quando:
            cont["ja_lidos"] += 1
            continue
        if isinstance(obj, Pessoa):
            cont[_aplicar_pessoa(db, obj, r, quando)] += 1
        else:
            chave, novos = _aplicar_empresa(db, obj, r, quando)
            cont[chave] += 1
            cont["decisores_novos"] += novos
    db.commit()
    return cont


def mudou_de_empresa(p: Pessoa) -> bool:
    if not p.linkedin_empresa_atual or not p.empresa:
        return False
    atual = set(normalizar_nome_empresa(p.linkedin_empresa_atual).split())
    nossa = set(normalizar_nome_empresa(p.empresa.nome_fantasia or p.empresa.razao_social).split())
    return bool(atual) and bool(nossa) and not (atual & nossa)
