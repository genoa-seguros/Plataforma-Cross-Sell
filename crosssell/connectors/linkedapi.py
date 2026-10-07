"""Cliente da Linked API (api.linkedapi.io), chamada direto pela plataforma, sem n8n.

Cada consulta é um "workflow" assíncrono: POST /workflows devolve um workflowId e
GET /workflows/{id} diz se terminou (workflowStatus completed/failed) e traz o
resultado em `completion`. A plataforma não espera: guarda o id e confere na
rodada seguinte (linkedin.executar).

Formato conferido com o SDK oficial (@linkedapi/node 2.3) e com respostas reais.
"""

import re

import httpx


BASE = "https://api.linkedapi.io"


class LinkedApiErro(RuntimeError):
    def __init__(self, tipo: str, mensagem: str):
        super().__init__(f"{tipo}: {mensagem}" if tipo else mensagem)
        self.tipo = tipo or ""


class LinkedApiClient:
    def __init__(self, token: str, identificacao: str, base: str = BASE, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=base, timeout=30, transport=transport, headers={
            "Content-Type": "application/json", "linked-api-token": token,
            "identification-token": identificacao, "client": "crosssell"})

    def _resposta(self, r: httpx.Response) -> dict:
        try:
            corpo = r.json()
        except ValueError:
            corpo = {}
        erro = corpo.get("error") if isinstance(corpo, dict) else None
        if erro or r.status_code >= 400:
            erro = erro or {}
            raise LinkedApiErro(erro.get("type") or f"http{r.status_code}", erro.get("message") or r.reason_phrase)
        if not corpo.get("result"):
            raise LinkedApiErro("semResultado", "a Linked API respondeu sem resultado")
        return corpo["result"]

    def _chamar(self, metodo: str, caminho: str, **kwargs) -> dict:
        try:
            r = self.http.request(metodo, caminho, **kwargs)
        except httpx.HTTPError as exc:  # timeout, conexão recusada: tratado como os erros da própria API
            raise LinkedApiErro("conexao", str(exc) or type(exc).__name__) from exc
        return self._resposta(r)

    def iniciar(self, definicao: dict) -> str:
        return self._chamar("POST", "/workflows", json=definicao)["workflowId"]

    def consultar(self, workflow_id: str) -> dict:
        return self._chamar("GET", f"/workflows/{workflow_id}")


LIMITE_FUNCIONARIOS = 2500  # máximo da lista de funcionários do Sales Navigator por consulta


def pagina_sales_navigator(urn: str | None) -> str | None:
    """Endereço da empresa no Sales Navigator a partir do urn da página comum (urn:li:fsd_company:1035)."""
    m = re.search(r"(\d+)\s*$", urn or "")
    return f"https://www.linkedin.com/sales/company/{m.group(1)}" if m else None


def definicao(alvo: dict) -> dict:
    """Workflow da Linked API para um alvo da plataforma (ver linkedin.alvos)."""
    tipo, acao = alvo["tipo"], alvo["acao"]
    if acao == "praca":
        # Sales Navigator: funcionários sem filtro, com o local de cada um; a plataforma conta quantos estão
        # em cidades alvo (o filtro de local do LinkedIn usa regiões próprias, a lista é a nossa)
        return {"actionType": "nv.openCompanyPage", "companyHashedUrl": alvo["sales_url"], "basicInfo": True,
                "then": [{"actionType": "nv.retrieveCompanyEmployees", "limit": alvo["limite"]}]}
    if acao == "pessoas":
        return {"actionType": "nv.openCompanyPage", "companyHashedUrl": alvo["sales_url"], "basicInfo": True,
                "then": [{"actionType": "nv.retrieveCompanyEmployees", "limit": 25,
                          "filter": {"positions": alvo["cargos"]}}]}
    if acao == "ler" and tipo == "pessoa" and "/sales/" in alvo["linkedin_url"]:
        return {"actionType": "nv.openPersonPage", "personHashedUrl": alvo["linkedin_url"], "basicInfo": True, "then": []}
    if acao == "buscar" and tipo == "pessoa":
        # O termo é obrigatório. O filtro de empresa atual não é confiável com o nome da empresa
        # (testado: "SUMUP" não achou a CEO da SumUp); a empresa é conferida no headline.
        return {"actionType": "st.searchPeople", "term": alvo["nome"], "limit": 10}
    if acao == "buscar":
        return {"actionType": "st.searchCompanies", "term": alvo["busca"], "limit": 10}
    if acao == "area":
        # Lista de funcionários; o filtro de cargo é ignorado pela Linked API (testado), então
        # vem uma lista maior e a plataforma classifica cada um pela área do título.
        return {"actionType": "st.openCompanyPage", "companyUrl": alvo["linkedin_url"], "basicInfo": True,
                "then": [{"actionType": "st.retrieveCompanyEmployees", "limit": 50}]}
    if tipo == "pessoa":
        return {"actionType": "st.openPersonPage", "personUrl": alvo["linkedin_url"], "basicInfo": True, "then": []}
    return {"actionType": "st.openCompanyPage", "companyUrl": alvo["linkedin_url"], "basicInfo": True,
            "then": [{"actionType": "st.retrieveCompanyDMs", "limit": 20},
                     {"actionType": "st.retrieveCompanyPosts", "limit": 5}]}


def _lista(v) -> list:
    return v if isinstance(v, list) else ([v] if isinstance(v, dict) else [])


def _pessoa_curta(x: dict) -> dict:
    return {"nome": x.get("name") or "", "headline": x.get("headline") or x.get("position") or "",
            "linkedin_url": x.get("publicUrl") or x.get("hashedUrl") or "", "local": x.get("location") or ""}


def resultado(id_alvo: str, tipo: str, acao: str, area: str | None, completion) -> dict:
    """Converte a `completion` da Linked API no formato de linkedin.receber."""
    base = {"id_alvo": id_alvo, "tipo": tipo, "acao": acao}
    c = _lista(completion)[0] if _lista(completion) else {}
    if c.get("error") or not c.get("data"):
        erro = c.get("error") or {}
        return {**base, "erro": erro.get("message") or erro.get("type") or "sem dados"}
    d = c["data"]
    if acao == "buscar":
        itens = d if isinstance(d, list) else _lista(d)
        return {**base, "candidatos": [{**_pessoa_curta(x), "setor": x.get("industry") or ""} for x in itens]}
    then = {t.get("actionType"): t.get("data") for t in _lista(d.get("then"))}
    if acao in ("praca", "pessoas"):
        erro = next((t.get("error") for t in _lista(d.get("then")) if t.get("error")), None)
        if erro:
            return {**base, "erro": erro.get("message") or erro.get("type") or "falhou"}
        lista = [_pessoa_curta(x) for x in _lista(then.get("nv.retrieveCompanyEmployees"))]
        if acao == "praca":
            return {**base, "total": d.get("employeesCount"), "locais": [x["local"] for x in lista]}
        return {**base, "funcionarios_area": lista}
    if acao == "area":
        return {**base, "area": area,
                "funcionarios_area": [_pessoa_curta(x) for x in _lista(then.get("st.retrieveCompanyEmployees"))]}
    if tipo == "pessoa":
        return {**base, "linkedin_url": d.get("publicUrl") or "", "nome": d.get("name") or "",
                "headline": d.get("headline") or "", "cargo_atual": d.get("position") or "",
                "empresa_atual": d.get("companyName") or "", "localizacao": d.get("location") or ""}
    return {**base, "linkedin_url": d.get("publicUrl") or "", "nome": d.get("name") or "", "urn": d.get("urn") or "",
            "setor": d.get("industry") or "", "funcionarios": d.get("employeesCount"),
            "site": d.get("website") or "", "sede": d.get("headquarters") or d.get("location") or "",
            "descricao": " · ".join(x for x in (d.get("description"), d.get("specialties")) if x),
            "investida": d.get("ventureFinancing"),
            "decisores": [_pessoa_curta(x) for x in _lista(then.get("st.retrieveCompanyDMs"))],
            "posts": [{"texto": p.get("text") or "", "data": p.get("time") or "", "url": p.get("url") or ""}
                      for p in _lista(then.get("st.retrieveCompanyPosts"))]}
