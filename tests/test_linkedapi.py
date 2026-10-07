"""Chamada direta da Linked API, com respostas no formato real (CLI/SDK conferidos)."""

import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from crosssell import potencial, tabela
from crosssell.connectors import linkedin as lk
from crosssell.connectors.linkedapi import LinkedApiClient, definicao, resultado
from crosssell.models import Empresa, LinkedinPedido, Pessoa
from tests.test_fluxo import carregar

EMPRESA_ALFA = {
    "name": "Metalúrgica Alfa", "publicUrl": "https://www.linkedin.com/company/metalurgica-alfa",
    "industry": "Metalurgia", "employeesCount": 850, "urn": "urn:li:fsd_company:1035", "website": "https://alfa.com.br", "headquarters": "Joinville, SC",
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
    {"name": "Davi Rocha", "headline": "Analista Fiscal", "publicUrl": "https://www.linkedin.com/in/davi"},  # Financeiro
    # Lista real da Salvy trazia investidores: título que cita outra organização fica de fora
    {"name": "Rony Sz", "headline": "Co-founder @ STAMINA VC", "publicUrl": "https://www.linkedin.com/in/rony"},
    {"name": "Eva Lins", "headline": "Vendedora", "publicUrl": "https://www.linkedin.com/in/eva"},  # sem área
]


class LinkedApiFalsa:
    """POST /workflows -> id; GET /workflows/{id} -> completed com a resposta do tipo pedido."""

    def __init__(self, pendentes: int = 0):
        self.pedidos: dict[str, dict] = {}
        self.headers = []
        self.pendentes = pendentes  # quantas consultas responder "running" antes de concluir
        self.falhas: dict[tuple[str, int], Exception] = {}  # (método, nº da chamada) -> exceção a levantar
        self.chamadas = {"POST": 0, "GET": 0}
        # locais dos funcionários no Sales Navigator: 4 de 6 com cidade em cidades alvo (o "Brazil" não conta)
        self.locais = ["São Paulo, São Paulo, Brazil", "Greater São Paulo Area", "Barueri, São Paulo, Brazil",
                       "Rio de Janeiro e Região", "Joinville, Santa Catarina, Brazil", "Blumenau, SC", "Brazil"]

    def _resposta(self, d: dict):
        t = d["actionType"]
        if t == "st.searchCompanies":
            return [{"name": "Metalurgica Alfa", "publicUrl": "https://www.linkedin.com/company/metalurgica-alfa",
                     "location": "Joinville, SC", "industry": "Metalurgia"},
                    {"name": "Beta Serviços", "publicUrl": "https://www.linkedin.com/company/beta", "location": "São Paulo, SP"}]
        if t == "st.searchPeople":
            return [{"name": "Ana Souza", "headline": "Advogada", "publicUrl": "https://www.linkedin.com/in/ana-adv"},
                    {"name": "Ana Souza", "headline": "CFO | Metalúrgica Alfa", "publicUrl": "https://www.linkedin.com/in/ana-cfo"}]
        if t == "nv.openCompanyPage":
            filtro = d["then"][0].get("filter")
            lista = ([{"name": "Ana Souza", "position": "CFO", "hashedUrl": "https://www.linkedin.com/sales/lead/ana"},
                      {"name": "Carla Mendes", "position": "Diretora de RH", "hashedUrl": "https://www.linkedin.com/sales/lead/carla"}]
                     if filtro else [{"name": f"F{i}", "position": "Operador", "location": local}
                                     for i, local in enumerate(self.locais)])
            return {"name": "Metalúrgica Alfa", "employeesCount": 850,
                    "then": [{"actionType": "nv.retrieveCompanyEmployees", "success": True, "data": lista}]}
        if t == "st.openCompanyPage" and d["then"] and d["then"][0]["actionType"] == "st.retrieveCompanyEmployees":
            lista = [] if "beta" in d["companyUrl"] else FUNCIONARIOS_RH  # cada empresa com os seus funcionários
            return {**{k: v for k, v in EMPRESA_ALFA.items() if k != "then"},
                    "then": [{"actionType": "st.retrieveCompanyEmployees", "success": True, "data": lista}]}
        if t == "st.openCompanyPage" and "beta" in d["companyUrl"]:
            return {"name": "Beta Serviços", "publicUrl": d["companyUrl"], "employeesCount": 40, "then": []}
        if t == "st.openCompanyPage":
            return EMPRESA_ALFA
        return {"name": "Ana Souza", "headline": "CFO | Metalúrgica Alfa", "publicUrl": "https://www.linkedin.com/in/ana-cfo",
                "position": "CFO", "companyName": "Metalúrgica Alfa", "location": "Joinville, SC"}

    def client(self) -> LinkedApiClient:
        def handler(req: httpx.Request):
            self.headers.append(dict(req.headers))
            self.chamadas[req.method] += 1
            if (req.method, self.chamadas[req.method]) in self.falhas:
                raise self.falhas[(req.method, self.chamadas[req.method])]
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
    assert area["then"] == [{"actionType": "st.retrieveCompanyEmployees", "limit": 50}]  # filtro de cargo é ignorado


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
    areas = sorted(p["def"]["companyUrl"].rsplit("/", 1)[1] for p in api.pedidos.values() if p["def"].get("then")
                   and p["def"]["then"][0]["actionType"] == "st.retrieveCompanyEmployees")
    # Beta (sem oportunidade de Saúde) segue a leitura geral: falta Operações para RE.
    # Alfa tem oportunidade de Saúde, mas a sede (Joinville) não é cidade alvo e sem Sales Navigator não dá para
    # ver onde estão os funcionários: quem decide só é procurado quando a praça estiver decidida
    assert areas == ["beta"]
    assert alfa.cidade == "Joinville, SC" and alfa.cidade_fonte == "linkedin"
    alfa.cidade, alfa.uf, alfa.cidade_fonte = "São Paulo", "SP", "manual"  # informada à mão: decide a praça
    db.commit()
    lk.executar(db, s, client)
    areas = sorted(p["def"]["companyUrl"].rsplit("/", 1)[1] for p in api.pedidos.values() if p["def"].get("then")
                   and p["def"]["then"][0]["actionType"] == "st.retrieveCompanyEmployees")
    assert areas == ["beta", "metalurgica-alfa"]
    lk.executar(db, s, client)
    lk.executar(db, s, client)
    db.expire_all()
    carla = db.scalar(select(Pessoa).where(Pessoa.nome == "Carla Mendes"))
    assert carla and carla.fonte == "linkedin" and carla.empresa_id == alfa.id
    assert db.scalar(select(Pessoa).where(Pessoa.nome == "Davi Rocha")).empresa_id == alfa.id  # área Financeiro
    assert db.scalar(select(Pessoa).where(Pessoa.nome == "Rony Sz")) is None  # investidor de outra organização
    assert db.scalar(select(Pessoa).where(Pessoa.nome == "Eva Lins")) is None  # título sem área

    # Na linha de Saúde da Alfa aparece quem decide (RH) e, sem relação, a ponte
    pipo = next(x for x in tabela.montar(db, s) if x["pipedriveId"] == "11")
    assert [p["nome"] for p in pipo["quemDecide"]["pessoas"]] == ["Carla Mendes"]
    assert any(m["texto"].startswith("RH estruturado: Carla Mendes") for m in pipo["motivos"])
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


def test_queda_de_conexao_nao_perde_pedidos_pagos(db, settings):
    s = config(settings)
    carregar(db, s)
    api = LinkedApiFalsa(pendentes=99)
    api.falhas[("POST", 3)] = httpx.ConnectTimeout("timeout")  # 3º pedido sem resposta: conta como falha
    r = lk.executar(db, s, api.client())
    assert r["iniciados"] == 3 and r["falhas"] == 1 and r["erro"].startswith("conexao")
    assert len(db.scalars(select(LinkedinPedido)).all()) == 3


def test_erro_no_meio_do_lote_mantem_os_pedidos_ja_feitos(db, settings):
    s = config(settings)
    carregar(db, s)
    api = LinkedApiFalsa(pendentes=99)
    api.falhas[("POST", 3)] = RuntimeError("quebrou")
    try:
        lk.executar(db, s, api.client())
    except RuntimeError:
        pass
    db.rollback()
    assert {p.workflow_id for p in db.scalars(select(LinkedinPedido))} == {"wf-1", "wf-2"}
    assert lk.executar(db, s, api.client())["iniciados"] == 2  # não repete os dois que já estavam em andamento


def test_queda_de_conexao_ao_consultar_mantem_o_pedido(db, settings):
    s = config(settings, linkedin_limite_dia=1)
    carregar(db, s)
    api = LinkedApiFalsa()
    lk.executar(db, s, api.client())
    api.falhas[("GET", 1)] = httpx.ReadTimeout("timeout")
    r = lk.executar(db, s, api.client())
    assert r["andamento"] == 1 and r["erros"] == 0
    assert db.scalar(select(LinkedinPedido)).situacao == "pendente"
    assert lk.executar(db, s, api.client())["aplicados"] == 1  # na rodada seguinte o resultado é aplicado


def test_funcionarios_reais_da_salvy(db, settings):
    """Lista real (filtro de cargo ignorado pela Linked API): entram fundadores e quem tem área;
    investidores de outra organização e títulos sem área ficam de fora."""
    salvy = Empresa(razao_social="Salvy Tecnologia Ltda", nome_fantasia="Salvy", nome_normalizado="salvy",
                    linkedin_url="https://www.linkedin.com/company/use-salvy")
    db.add(salvy)
    db.commit()
    lista = [("William Cordeiro", "Managing Partner @SaaSholic"),
             ("Lucas Rosa", "Co-Founder & Product @ Salvy (YC W24)"),
             ("Rony Sztamfater", "Co-founder @ STAMINA VC"),
             ("Yuri Enny", "Marketing Lead @ Salvy (YC W24)"),
             ("Artur Negrão", "Co-founder & CEO, Salvy"),
             ("Paulo Cunha", "Investidor Anjo de Empreendedores Brasileiros")]
    lk.receber(db, [{"id_alvo": f"E{salvy.id}", "tipo": "empresa", "acao": "area", "area": "rh",
                     "funcionarios_area": [{"nome": n, "headline": h, "linkedin_url": f"https://x/{i}"}
                                           for i, (n, h) in enumerate(lista)],
                     "capturado_em": "2026-10-04T10:00:00"}])
    assert sorted(p.nome for p in salvy.pessoas) == ["Artur Negrão", "Lucas Rosa"]
    assert "funcionarios" in salvy.linkedin_areas


def test_saude_com_sales_navigator_decide_a_praca_e_acha_quem_decide(db, settings):
    s = config(settings, linkedin_sales_navigator=True)
    carregar(db, s)
    api = LinkedApiFalsa()
    client = api.client()
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    for _ in range(3):  # busca, leitura da página (com o urn) e a praça pelo Sales Navigator
        lk.executar(db, s, client)
    db.expire_all()
    assert alfa.cidade == "Joinville, SC" and alfa.linkedin_areas["urn"] == "urn:li:fsd_company:1035"
    praca = next(p["def"] for p in api.pedidos.values() if p["def"]["actionType"] == "nv.openCompanyPage")
    assert praca["companyHashedUrl"] == "https://www.linkedin.com/sales/company/1035"
    assert praca["then"] == [{"actionType": "nv.retrieveCompanyEmployees", "limit": 850}]
    lk.executar(db, s, client)
    db.expire_all()
    # 4 de 6 funcionários com cidade estão em cidades alvo: 67% >= 30% -> alvo, mesmo com a matriz em Joinville
    assert alfa.praca == "alvo" and alfa.praca_fatia == 0.667
    assert potencial.praca(alfa) == "alvo"
    # Na praça: lista de quem decide por cargo (Sales Navigator), paga pela cota de pessoas de Saúde
    pessoas = [p["def"] for p in api.pedidos.values() if p["def"]["actionType"] == "nv.openCompanyPage"
               and p["def"]["then"][0].get("filter")]
    assert len(pessoas) == 1 and "diretor de rh" in pessoas[0]["then"][0]["filter"]["positions"]
    # Paga a vertical de maior Score da Alfa (a leitura serve a todas); grupo "pessoas" só na cota de Saúde
    ped = db.scalar(select(LinkedinPedido).where(LinkedinPedido.acao == "pessoas"))
    paga = lk._candidatos_alvo(db, s)[2][alfa.id][0]
    assert ped.vertical == paga and ped.grupo == ("pessoas" if paga == "saude" else "geral")
    lk.executar(db, s, client)
    db.expire_all()
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    carla = db.scalar(select(Pessoa).where(Pessoa.nome == "Carla Mendes"))
    assert ana.linkedin_headline == "CFO"  # contato do e-mail ganha o cargo atual
    assert carla.empresa_id == alfa.id and carla.linkedin_url.endswith("/sales/lead/carla")
    assert not [a for a in lk.alvos(db, s) if a["id_alvo"] == f"E{alfa.id}"]  # nada mais até a validade
    # Depois de 180 dias, relê a empresa, a praça e a lista
    depois = datetime.utcnow() + timedelta(days=181)
    assert {a["acao"] for a in lk.alvos(db, s, depois) if a["id_alvo"] == f"E{alfa.id}"} == {"ler", "praca", "pessoas"}


def test_fora_da_praca_nao_gasta_consulta_com_pessoas(db, settings):
    s = config(settings, linkedin_sales_navigator=True)
    carregar(db, s)
    api = LinkedApiFalsa()
    api.locais = ["Joinville, Santa Catarina, Brazil"] * 8 + ["São Paulo, São Paulo, Brazil"]
    for _ in range(5):
        lk.executar(db, s, api.client())
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    assert alfa.praca == "fora" and alfa.praca_fatia == 0.111
    assert not any(p["def"]["then"][0].get("filter") for p in api.pedidos.values()
                   if p["def"]["actionType"] == "nv.openCompanyPage")
    assert potencial.praca(alfa) == "fora" and potencial.faltando_saude(alfa) == []  # analisada, fora da praça


def test_cidade_alvo_dispensa_a_consulta_da_praca(db, settings):
    s = config(settings, linkedin_sales_navigator=True)
    carregar(db, s)
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    alfa.cidade, alfa.uf, alfa.cidade_fonte = "Campinas", "SP", "manual"  # fora da lista, mas à mão: decide
    alfa.funcionarios, alfa.funcionarios_fonte, alfa.funcionarios_em = 300, "manual", datetime.utcnow()
    alfa.setor = "Metalurgia"
    db.commit()
    assert [a for a in lk.alvos(db, s) if a["id_alvo"] == f"E{alfa.id}"] == []  # nem página, nem praça
    alfa.linkedin_url = "https://www.linkedin.com/company/metalurgica-alfa"
    db.commit()
    assert not [a for a in lk.alvos(db, s) if a["id_alvo"] == f"E{alfa.id}"]  # fora da praça: nem pessoas


def test_cotas_por_vertical_e_sobra():
    cotas = {("saude", "caracteristicas"): 2, ("saude", "pessoas"): 1, ("linhas_financeiras", "geral"): 1,
             ("ramos_elementares", "geral"): 1}
    fila = ([{"id_alvo": f"S{i}", "vertical": "saude", "grupo": "caracteristicas"} for i in range(5)]
            + [{"id_alvo": "P1", "vertical": "saude", "grupo": "pessoas"}]
            + [{"id_alvo": f"L{i}", "vertical": "linhas_financeiras", "grupo": "geral"} for i in range(3)])
    ids = lambda xs: [a["id_alvo"] for a in xs]  # noqa: E731
    # Alterna entre as cotas com saldo, na ordem do Score
    assert ids(lk.escolher(fila, cotas, {}, 4)) == ["S0", "P1", "L0", "S1"]
    # RE não tem o que fazer: a sobra vai para os próximos da fila
    assert ids(lk.escolher(fila, cotas, {}, 7)) == ["S0", "P1", "L0", "S1", "S2", "S3", "S4"]
    # Cota de Saúde já usada nas últimas 24 h: vai LF primeiro
    assert ids(lk.escolher(fila, cotas, {("saude", "caracteristicas"): 2, ("saude", "pessoas"): 1}, 2)) == ["L0", "S0"]
    assert lk.escolher(fila, cotas, {}, 0) == []


def test_resultado_do_sales_navigator():
    praca = resultado("E1", "empresa", "praca", None, {"actionType": "nv.openCompanyPage", "success": True, "data": {
        "name": "X", "employeesCount": 120, "then": [{"actionType": "nv.retrieveCompanyEmployees", "success": True,
                                                       "data": [{"name": "A", "location": "Recife, Pernambuco, Brazil"}]}]}})
    assert praca["total"] == 120 and praca["locais"] == ["Recife, Pernambuco, Brazil"]
    falha = resultado("E1", "empresa", "pessoas", None, {"actionType": "nv.openCompanyPage", "success": True, "data": {
        "name": "X", "then": [{"actionType": "nv.retrieveCompanyEmployees", "success": False,
                               "error": {"type": "noSalesNavigator", "message": "Sem Sales Navigator"}}]}})
    assert falha["erro"] == "Sem Sales Navigator"
    assert definicao({"tipo": "pessoa", "acao": "ler", "linkedin_url": "https://www.linkedin.com/sales/lead/x"}) == \
        {"actionType": "nv.openPersonPage", "personHashedUrl": "https://www.linkedin.com/sales/lead/x",
         "basicInfo": True, "then": []}


def test_comando_de_teste_do_sales_navigator(db, settings):
    s = config(settings)
    carregar(db, s)
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    alfa.linkedin_url = "https://www.linkedin.com/company/metalurgica-alfa"
    db.commit()
    linhas, client = [], LinkedApiFalsa().client()
    r = lk.testar_sales_navigator(db, s, alfa, client, saida=linhas.append, espera=0)
    texto = "\n".join(linhas)
    assert r == {"praca": "alvo", "fatia": 4 / 6, "pessoas": 2, "aplicado": False}
    assert "sales/company/1035" in texto and "← cidade alvo" in texto and "Carla Mendes · Diretora de RH" in texto
    db.expire_all()
    assert alfa.praca is None  # sem --aplicar não grava
    assert len(db.scalars(select(LinkedinPedido)).all()) == 3  # página, praça e pessoas contam no limite de 24 h
    lk.testar_sales_navigator(db, s, alfa, client, aplicar=True, saida=linhas.append, espera=0)
    db.expire_all()
    assert alfa.praca == "alvo" and alfa.linkedin_areas["urn"] and "Carla Mendes" in {p.nome for p in alfa.pessoas}
