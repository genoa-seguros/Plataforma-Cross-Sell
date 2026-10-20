"""Fila de análise (completar à mão e levar para Oportunidades), ponte marcada à mão e notas da equipe que a IA
lê para ajustar o Score."""

import json

from crosssell import nota_ia
from crosssell.models import Empresa, Negocio
from crosssell.web import app as webapp
from tests.test_fluxo import _ClaudeFalso
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


def _lead(db) -> Empresa:
    """Lead negociando D&O, na praça, com funcionários e setor: em Saúde falta quem decide e a ponte."""
    e = Empresa(razao_social="Capim Tecnologia SA", nome_normalizado="capim tecnologia", cidade="São Paulo", uf="SP",
                funcionarios=300, funcionarios_fonte="linkedin", setor="Fintech", setor_fonte="linkedin")
    db.add_all([e, Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo="cp1", pipeline_id=1,
                           status="aberto", titulo="D&O")])
    db.commit()
    return e


def test_fila_completar_a_mao_e_mover_para_oportunidades(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = _lead(db)
    fila = c.get("/api/fila?vertical=saude&q=capim").json()
    item = fila["itens"][0]
    assert item["faltando"] == ["quem decide", "ponte por e-mail"] and item["pessoas"] == []

    # Quem decide cadastrado à mão e marcado como nossa ponte (conhecida pessoal): vira oportunidade analisada
    rh = c.post(f"/api/empresas/{e.id}/pessoas", headers=H, json={"nome": "Rita Souza", "cargo": "Gerente de RH"}).json()
    assert rh["area"] == "RH / Pessoas"
    assert c.post(f"/api/pessoas/{rh['id']}/ponte", headers=H, json={"ponte": True}).json() == {"ponteManual": True}
    ops = c.get("/api/oportunidades?vertical=saude&q=capim").json()["itens"]
    assert ops and ops[0]["analisada"] and ops[0]["quemDecide"]["pessoas"][0]["ponteManual"]
    assert not c.get("/api/fila?vertical=saude&q=capim").json()["itens"]

    # Levar à mão para Oportunidades, mesmo faltando algo (LF: Receita, notícias, cargos-chave...)
    assert c.get("/api/fila?vertical=linhas_financeiras&q=capim").json()["itens"]
    assert c.post("/api/oportunidades/manual", headers=H,
                  json={"empresa_id": e.id, "vertical": "linhas_financeiras"}).json() == {"ok": True}
    lf = next(o for o in c.get("/api/oportunidades?q=capim&oferta=mais").json()["itens"] if o["vertical"] == "linhas_financeiras")
    assert lf["manual"]["por"] == "Rodrigo" and lf["faltando"]  # mostra quem levou e o que ainda falta
    c.post("/api/oportunidades/manual", headers=H, json={"empresa_id": e.id, "vertical": "linhas_financeiras", "incluir": False})
    assert c.get("/api/fila?vertical=linhas_financeiras&q=capim").json()["itens"]  # desfeito: volta para a fila


def test_nota_da_equipe_ajusta_o_score_pela_ia(cenario, db, settings, monkeypatch):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = _lead(db)
    falso = _ClaudeFalso(json.dumps({"ajuste": -80, "motivo": "Corretor da família: entrada muito difícil.}"}))
    monkeypatch.setattr(webapp, "leitor_notas", lambda db: nota_ia.LeitorNotas(settings, client=falso, modelo="m"))
    antes = {o["vertical"]: o["score"] for o in c.get(f"/api/oportunidades?empresa={e.id}").json()["itens"]}

    r = c.post(f"/api/empresas/{e.id}/notas", headers=H,
               json={"vertical": "saude", "texto": "A Capim tem um corretor da família, muito difícil entrar."}).json()
    assert r["ajuste"] == -50 and r["motivo"] == "Corretor da família: entrada muito difícil."  # limite de -50
    assert "corretor da família" in falso.chamadas[0]["messages"][0]["content"]
    depois = {o["vertical"]: o for o in c.get(f"/api/oportunidades?empresa={e.id}").json()["itens"]}
    assert depois["saude"]["score"] == max(0, round(antes["saude"] - 50, 1))
    assert depois["saude"]["motivos"][0] == {"texto": "atualização da equipe (-50): Corretor da família: entrada muito difícil.",
                                            "sinal": "-"}
    assert depois["saude"]["notas"][0]["autor"] == "Rodrigo"
    assert depois["ramos_elementares"]["score"] == antes["ramos_elementares"]  # vale só para a vertical da nota

    # IA fora do ar: a nota fica salva sem ajuste e a rotina avalia depois
    monkeypatch.setattr(webapp, "leitor_notas", lambda db: (_ for _ in ()).throw(RuntimeError("fora do ar")))
    r = c.post(f"/api/empresas/{e.id}/notas", headers=H, json={"vertical": "ramos_elementares", "texto": "Pediu cotação de fiança."}).json()
    assert r["ajuste"] is None
    falso2 = _ClaudeFalso(json.dumps({"ajuste": 15, "motivo": "Pediu cotação."}))
    assert nota_ia.pendentes(db, nota_ia.LeitorNotas(settings, client=falso2, modelo="m")) == {"avaliadas": 1, "falhas": 0}


def test_completar_dados_lista_o_que_falta_por_responsavel(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = _lead(db)
    e.negocios[0].responsavel_email = "victor.boldrini@innoaseguros.com.br"
    db.commit()
    # LF levada à mão faltando dados: entra no relatório com o que falta e o responsável do card
    c.post("/api/oportunidades/manual", headers=H, json={"empresa_id": e.id, "vertical": "linhas_financeiras"})
    # Saúde analisada, mas a ponte foi cadastrada sem cargo
    c.post(f"/api/empresas/{e.id}/pessoas", headers=H, json={"nome": "Rita Souza", "cargo": "Gerente de RH"})
    s = c.post(f"/api/empresas/{e.id}/pessoas", headers=H, json={"nome": "Caio Lima", "cargo": ""}).json()
    c.post(f"/api/pessoas/{s['id']}/ponte", headers=H, json={"ponte": True})

    r = c.get("/api/pendencias").json()
    por_vertical = {o["vertical"]: o for o in r["itens"]}
    lf = por_vertical["linhas_financeiras"]
    assert lf["donos"] == ["victor.boldrini@innoaseguros.com.br"] and lf["manual"]
    # só o que dá para preencher à mão (Receita, notícias e site a rotina lê sozinha)
    assert [x["campo"] for x in lf["pendencias"]] == ["quem_decide", "cargo"]
    assert r["totalPendencias"] == len(r["itens"])
    assert {"dono": "victor.boldrini@innoaseguros.com.br", "nome": "Victor", "n": len(r["itens"])} in [
        {**x, "nome": x["nome"].split()[0]} for x in r["resumo"]]
    assert c.get("/api/oportunidades?oferta=mais").json()["pendencias"] >= 1
    assert not c.get("/api/pendencias?dono=-").json()["itens"]  # todos têm responsável
    assert c.get("/api/pendencias?vertical=linhas_financeiras&q=capim").json()["itens"][0]["id"] == lf["id"]
    assert por_vertical["saude"]["pendencias"] == [{"campo": "cargo", "texto": "cargo de Caio Lima",  # a ponte sem cargo
                                                    "pessoa": {"id": s["id"], "nome": "Caio Lima"}}]


def test_pendencias_so_campos_vazios():
    from crosssell import tabela
    ponte = {"id": 1, "nome": "Ana", "cargo": "CFO", "relacao": 40}
    base = {"analisada": True, "faltando": [], "manual": None, "empresa": {"local": "SP", "funcionarios": 10, "setor": "x"},
            "quemDecide": {"areas": ["Financeiro"], "pessoas": [ponte], "daArea": True,
                           "contato": {"id": 2, "nome": "Mayane", "cargo": None, "relacao": 0}}}
    assert tabela.pendencias(base) == []  # tudo capturado; o cargo do contato do Pipedrive não é pedido
    assert tabela.pendencias({**base, "analisada": False}) == []
    sem_cargo = {**base, "quemDecide": {**base["quemDecide"], "pessoas": [{**ponte, "cargo": None}]}}
    assert [x["texto"] for x in tabela.pendencias(sem_cargo)] == ["cargo de Ana"]
    so_pipedrive = {**base, "quemDecide": {**base["quemDecide"], "pessoas": [{**ponte, "relacao": 0}]}}
    assert [x["campo"] for x in tabela.pendencias(so_pipedrive)] == ["ponte"]
    executivo = {**base, "quemDecide": {**base["quemDecide"], "daArea": False}}
    assert [x["texto"] for x in tabela.pendencias(executivo)] == ["quem decide de Financeiro"]
    vazia = {**base, "empresa": {"local": None, "funcionarios": None, "setor": None}}
    assert [x["campo"] for x in tabela.pendencias(vazia)] == ["cidade", "funcionarios", "setor"]
