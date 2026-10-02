"""Chamada direta da Linked API, com respostas no formato real (CLI/SDK conferidos)."""

import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from crosssell import tabela
from crosssell.connectors import linkedin as lk
from crosssell.connectors.linkedapi import LinkedApiClient, definicao, resultado
from crosssell.models import Empresa, LinkedinPedido, Pessoa
from tests.test_fluxo import carregar

EMPRESA_ALFA = {
    "name": "Metalúrgica Alfa", "publicUrl": "https://www.linkedin.com/company/metalurgica-alfa",
    "industry": "Metalurgia", "employeesCount": 850, "website": "https://alfa.com.br", "headquarters": "Joinville, SC",
    "then": [
        {"actionType": "st.retrieveCompanyDMs", "success": True, "data": [
            {"name": "Ana Souza", "headline": "CFO | Metalúrgica Alfa", "publicUrl": "https://www.linkedin.com/in/ana-cfo"},
            {"name": "Bruno Prado", "headline": "Diretor de Operações na Metalúrgica Alfa",
             "publicUrl": "https://www.linkedin.com/in/bruno-prado"}]},
        {"actionType": "st.retrieveCompanyPosts", "success": True, "data": [
            {"url": "https://www.linkedin.com/posts/alfa-1", "time": "2026-09-28T10:00:00Z",
             "text": "Inauguramos a nova fábrica em Joinville\nMais 200 vagas"}]},
    ],
}
FUNCIONARIOS_RH = [
    {"name": "Carla Mendes", "headline": "Head de Pessoas e Cultura | Metalúrgica Alfa",
     "publicUrl": "https://www.linkedin.com/in/carla-mendes"},
    {"name": "Davi Rocha", "headline": "Analista Fiscal", "publicUrl": "https://www.linkedin.com/in/davi"},  # não é RH
]


class LinkedApiFalsa:
    """POST /workflows -> id; GET /workflows/{id} -> completed com a resposta do tipo pedido."""

    def __init__(self, pendentes: int = 0):
        self.pedidos: dict[str, dict] = {}
        self.headers = []
        self.pendentes = pendentes  # quantas consultas responder "running" antes de concluir

    def _resposta(self, d: dict):
        t = d["actionType"]
        if t == "st.searchCompanies":
            return [{"name": "Metalurgica Alfa", "publicUrl": "https://www.linkedin.com/company/metalurgica-alfa",
                     "location": "Joinville, SC", "industry": "Metalurgia"},
                    {"name": "Beta Serviços", "publicUrl": "https://www.linkedin.com/company/beta", "location": "São Paulo, SP"}]
        if t == "st.searchPeople":
            return [{"name": "Ana Souza", "headline": "Advogada", "publicUrl": "https://www.linkedin.com/in/ana-adv"},
                    {"name": "Ana Souza", "headline": "CFO | Metalúrgica Alfa", "publicUrl": "https://www.linkedin.com/in/ana-cfo"}]
        if t == "st.openCompanyPage" and d["then"] and d["then"][0]["actionType"] == "st.retrieveCompanyEmployees":
            return {**{k: v for k, v in EMPRESA_ALFA.items() if k != "then"},
                    "then": [{"actionType": "st.retrieveCompanyEmployees", "success": True, "data": FUNCIONARIOS_RH}]}
        if t == "st.openCompanyPage" and "beta" in d["companyUrl"]:
            return {"name": "Beta Serviços", "publicUrl": d["companyUrl"], "employeesCount": 40, "then": []}
        if t == "st.openCompanyPage":
            return EMPRESA_ALFA
        return {"name": "Ana Souza", "headline": "CFO | Metalúrgica Alfa", "publicUrl": "https://www.linkedin.com/in/ana-cfo",
                "position": "CFO", "companyName": "Metalúrgica Alfa", "location": "Joinville, SC"}

    def client(self) -> LinkedApiClient:
        def handler(req: httpx.Request):
            self.headers.append(dict(req.headers))
            if req.method == "POST":
                wid = f"wf-{len(self.pedidos) + 1}"
                self.pedidos[wid] = {"def": json.loads(req.content), "consultas": 0}
                return httpx.Response(200, json={"success": True, "result": {"workflowId": wid, "workflowStatus": "pending"}})
            wid = req.url.path.rsplit("/", 1)[1]
            ped = self.pedidos[wid]
            ped["consultas"] += 1
            if ped["consultas"] <= self.pendentes:
                return httpx.Response(200, json={"success": True, "result": {"workflowId": wid, "workflowStatus": "running"}})
            d = ped["def"]
            return httpx.Response(200, json={"success": True, "result": {
                "workflowId": wid, "workflowStatus": "completed",
                "completion": {"actionType": d["actionType"], "success": True, "data": self._resposta(d)}}})
        return LinkedApiClient("tok", "ident", transport=httpx.MockTransport(handler))


def config(settings, **extra):
    return settings.model_copy(update={"linked_api_token": "tok", "linked_api_identification_token": "ident",
                                       "linkedin_lote": 10, "linkedin_limite_dia": 50, **extra})


def test_definicoes_no_formato_da_linked_api():
    assert definicao({"tipo": "pessoa", "acao": "buscar", "nome": "Mariana Lazaro"}) == \
        {"actionType": "st.searchPeople", "term": "Mariana Lazaro", "limit": 10}
    assert definicao({"tipo": "pessoa", "acao": "ler", "linkedin_url": "https://www.linkedin.com/in/gustavo-dacol/"}) == \
        {"actionType": "st.openPersonPage", "personUrl": "https://www.linkedin.com/in/gustavo-dacol/",
         "basicInfo": True, "then": []}
    emp = definicao({"tipo": "empresa", "acao": "ler", "linkedin_url": "https://www.linkedin.com/company/use-salvy"})
    assert [t["actionType"] for t in emp["then"]] == ["st.retrieveCompanyDMs", "st.retrieveCompanyPosts"]
    area = definicao({"tipo": "empresa", "acao": "area", "area": "rh", "linkedin_url": "https://x"})
    assert area["then"][0]["filter"] == {"position": "RH"}


def test_resultado_pessoa_real_gustavo():
    """Saída real de `linkedin person fetch https://www.linkedin.com/in/gustavo-dacol/`."""
    completion = {"actionType": "st.openPersonPage", "success": True, "data": {
        "name": "Gustavo Dacol", "headline": "CEO na Jettax", "location": "São Paulo, Brasil",
        "companyName": "Jettax - Soluções em Automação", "publicUrl": "https://www.linkedin.com/in/gustavo-dacol",
        "position": "CEO", "countryCode": "BR", "then": []}}
    r = resultado("P1", "pessoa", "ler", None, completion)
    assert r["nome"] == "Gustavo Dacol" and r["cargo_atual"] == "CEO"
    assert r["empresa_atual"] == "Jettax - Soluções em Automação" and r["linkedin_url"].endswith("/gustavo-dacol")
    falha = resultado("P1", "pessoa", "ler", None, {"actionType": "st.openPersonPage", "success": False,
                                                    "error": {"type": "personNotFound", "message": "Perfil não encontrado"}})
    assert falha["erro"] == "Perfil não encontrado"


def test_rodadas_buscam_leem_e_procuram_rh(db, settings):
    s = config(settings)
    carregar(db, s)
    api = LinkedApiFalsa(pendentes=1)
    client = api.client()

    # Rodada 1: tudo sem endereço -> buscas (empresas e pessoas), em ordem de score
    r1 = lk.executar(db, s, client)
    assert r1["iniciados"] == 4 and r1["aplicados"] == 0
    assert api.headers[0]["linked-api-token"] == "tok" and api.headers[0]["identification-token"] == "ident"
    assert {p["def"]["actionType"] for p in api.pedidos.values()} == {"st.searchCompanies", "st.searchPeople"}
    assert lk.alvos(db, s) == []  # em andamento: não repete

    # Rodada 2: a Linked API ainda está processando -> nada aplicado, nada novo
    r2 = lk.executar(db, s, client)
    assert r2["andamento"] == 4 and r2["iniciados"] == 0

    # Rodada 3: concluídos -> Alfa achada pelo nome+local, Ana pela empresa no título
    r3 = lk.executar(db, s, client)
    assert r3["aplicados"] == 4
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    assert alfa.linkedin_url == "https://www.linkedin.com/company/metalurgica-alfa"
    assert ana.linkedin_url == "https://www.linkedin.com/in/ana-cfo" and ana.linkedin_headline == "CFO | Metalúrgica Alfa"
    # ...e na mesma rodada já pediu a leitura das páginas achadas (decisores e posts)
    leituras = {p["def"]["companyUrl"] for p in api.pedidos.values() if p["def"]["actionType"] == "st.openCompanyPage"}
    assert leituras == {alfa.linkedin_url, "https://www.linkedin.com/company/beta"}

    # Rodada 4 e 5: página lida -> funcionários, decisores, porte. A Alfa tem negócio de Saúde (Pipo)
    # e ninguém de RH -> pede os funcionários de RH
    lk.executar(db, s, client)
    lk.executar(db, s, client)
    db.expire_all()
    assert alfa.funcionarios == 850 and alfa.funcionarios_fonte == "linkedin"
    assert {p.nome for p in alfa.pessoas} >= {"Ana Souza", "Bruno Prado"}
    areas = sorted((p["def"]["companyUrl"].rsplit("/", 1)[1], p["def"]["then"][0]["filter"]["position"])
                   for p in api.pedidos.values() if p["def"].get("then")
                   and p["def"]["then"][0]["actionType"] == "st.retrieveCompanyEmployees")
    # Alfa: Saúde (Pipo) sem RH; RE já coberto pelo Diretor de Operações. Beta: Saúde e a oportunidade de RE
    assert areas == [("beta", "Operações"), ("beta", "RH"), ("metalurgica-alfa", "RH")]
    lk.executar(db, s, client)
    lk.executar(db, s, client)
    db.expire_all()
    carla = db.scalar(select(Pessoa).where(Pessoa.nome == "Carla Mendes"))
    assert carla and carla.fonte == "linkedin" and carla.empresa_id == alfa.id
    assert db.scalar(select(Pessoa).where(Pessoa.nome == "Davi Rocha")) is None  # cargo não é de RH

    # Na linha de Saúde da Alfa aparece quem decide (RH) e, sem relação, a ponte
    pipo = next(x for x in tabela.montar(db, s) if x["pipedriveId"] == "11")
    assert [p["nome"] for p in pipo["quemDecide"]["pessoas"]] == ["Carla Mendes"]
    assert any("quem decide Saúde: Carla Mendes" in m for m in pipo["motivos"])
    assert "rh" not in {a.get("area") for a in lk.alvos(db, s)}  # não procura RH de novo


def test_limite_de_24h(db, settings):
    s = config(settings, linkedin_limite_dia=3)
    carregar(db, s)
    api = LinkedApiFalsa(pendentes=99)
    assert lk.executar(db, s, api.client())["iniciados"] == 3
    r = lk.executar(db, s, api.client())
    assert r["iniciados"] == 0 and r["limite"]
    depois = datetime.utcnow() + timedelta(hours=25)
    r = lk.executar(db, s, api.client(), agora=depois)
    assert r["expirados"] == 3 and r["iniciados"] >= 1
    assert len(db.scalars(select(LinkedinPedido)).all()) >= 4
