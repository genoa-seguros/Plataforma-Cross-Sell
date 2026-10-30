"""Nova oportunidade a partir da organização (sem card aberto no Pipedrive) e Ganhos recentes por vertical."""

from datetime import date, timedelta

from crosssell.models import Empresa, Negocio
from crosssell.web import app as webapp
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


def test_criar_oportunidade_a_partir_da_organizacao(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = Empresa(razao_social="Gama Logística Ltda", nome_normalizado="gama logistica", pipedrive_org_id=777)
    neg = Empresa(razao_social="Delta Indústria SA", nome_normalizado="delta industria")
    db.add_all([e, neg, Negocio(empresa=neg, vertical="saude", fonte="pipedrive", id_externo="d1", pipeline_id=23,
                                status="aberto", titulo="Saúde")])
    db.commit()
    webapp.OPORTUNIDADES.limpar()
    assert not c.get(f"/api/oportunidades?empresa={e.id}").json()["itens"]  # sem card aberto: nada

    busca = c.get("/api/empresas?q=gama").json()["itens"]
    assert [x["nome"] for x in busca] == ["Gama Logística Ltda"] and busca[0]["cabe"]["saude"] is None
    assert c.get("/api/empresas?q=ga").json() == {"itens": []}  # menos de 3 letras
    assert c.post("/api/oportunidades/manual", headers=H, json={"empresa_id": e.id, "vertical": "saude"}).json() == {"ok": True}
    ops = c.get(f"/api/oportunidades?empresa={e.id}").json()["itens"]
    assert [o["vertical"] for o in ops] == ["saude"]  # só a vertical criada
    o = ops[0]
    assert o["analisada"] and o["manual"]["por"] == "Rodrigo" and not o["soGanho"]
    assert "Gama Logística Ltda" in {x["empresa"]["nome"] for x in c.get("/api/oportunidades?vertical=saude").json()["itens"]}
    funil = c.get("/api/oportunidades/funil?vertical=saude").json()["colunas"][0]
    assert "Gama Logística Ltda" in {x["empresa"]["nome"] for x in funil["cards"]}  # entra em Não atuamos

    # Já negocia a vertical: não cria
    r = c.post("/api/oportunidades/manual", headers=H, json={"empresa_id": neg.id, "vertical": "saude"})
    assert r.status_code == 400 and "já negocia" in r.json()["detail"]
    assert "já negocia" in c.get(f"/api/empresas/{neg.id}").json()["cabe"]["saude"]
    # Desfazer: a organização sai de Oportunidades
    c.post("/api/oportunidades/manual", headers=H, json={"empresa_id": e.id, "vertical": "saude", "incluir": False})
    assert not c.get(f"/api/oportunidades?empresa={e.id}").json()["itens"]


def test_ganhos_recentes_por_vertical(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    ontem = date.today() - timedelta(days=1)
    s, lf = Empresa(razao_social="Fechou Saúde SA", nome_normalizado="fechou saude"), \
        Empresa(razao_social="Fechou LF SA", nome_normalizado="fechou lf")
    db.add_all([s, lf,
                Negocio(empresa=s, vertical="saude", fonte="pipedrive", id_externo="g1", pipeline_id=23, status="ganho",
                        titulo="Saúde 2026", ganho_em=ontem),
                Negocio(empresa=lf, vertical="linhas_financeiras", fonte="pipedrive", id_externo="g2", pipeline_id=1,
                        status="ganho", titulo="D&O 2026", produto="D&O", ganho_em=ontem)])
    db.commit()
    webapp.OPORTUNIDADES.limpar()
    nomes = lambda r: {o["empresa"]["nome"] for o in r["itens"]}  # noqa: E731
    todos = c.get("/api/oportunidades?oferta=ganhos").json()
    assert {"Fechou Saúde SA", "Fechou LF SA"} <= nomes(todos)
    por = {g["vertical"]: g["n"] for g in todos["ganhosPorVertical"]}
    assert por["saude"] >= 1 and por["linhas_financeiras"] >= 1
    so_saude = c.get("/api/oportunidades?oferta=ganhos&ganho=saude").json()
    assert "Fechou Saúde SA" in nomes(so_saude) and "Fechou LF SA" not in nomes(so_saude)
    assert so_saude["ofertas"]["ganhos"] == so_saude["total"]
