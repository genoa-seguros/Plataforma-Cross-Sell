"""Diário da análise, filtros de To-dos (período e quem executa) e o filtro "Sem atividade" de Oportunidades."""

from datetime import date, timedelta

from sqlalchemy import select

from crosssell import tabela
from crosssell.connectors import pipedrive
from crosssell.models import AnaliseDia, Empresa, LinkedinPedido, Usuario
from tests.test_estacionar import _lead
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


def test_diario_mostra_quem_entrou_e_o_que_trava_a_fila(cenario, db, settings):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    a = _lead(db, 1)
    hoje = tabela.hoje_brasilia()
    # Ontem ninguém estava analisado em Saúde; hoje a Lead 1 entrou
    db.add(AnaliseDia(data=hoje - timedelta(days=1), vertical="saude", analisadas=0, fila=3, ids=[], faltando={}))
    db.add(LinkedinPedido(workflow_id="w1", id_alvo=f"E{a.id}", acao="pessoas", vertical="saude", situacao="aplicado",
                          concluido_em=tabela.datetime.utcnow()))
    db.commit()
    assert tabela.registrar_dia(db, settings)["saude"] == 1
    r = c.get("/api/diario").json()
    d = r["dias"][0]
    assert d["data"] == hoje.isoformat()
    assert d["verticais"]["saude"]["analisadas"] == 1 and d["verticais"]["saude"]["variacao"] == 1
    assert [e["nome"] for e in d["entraram"]] == ["Lead 1 SA"]
    assert d["linkedin"]["pedidos"] == 1 and d["linkedin"]["aplicados"] == 1
    # A fila de LF e RE (Receita, notícias, cargos-chave...) aparece no "o que trava"
    assert r["travas"]["linhas_financeiras"] and all(x["empresas"] > 0 for x in r["travas"]["linhas_financeiras"])


def test_todos_filtra_por_periodo_e_por_quem_executa(cenario, db):  # noqa: F811
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    bruno = db.scalar(select(Usuario).where(Usuario.email.like("bruno%")))
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    hoje = date.today()
    for assunto, dias, resp in (("Vencida", -3, victor), ("Hoje", 0, victor), ("Amanhã", 1, bruno), ("Mês que vem", 30, victor)):
        pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto=assunto, responsavel=resp,
                                  vencimento=hoje + timedelta(days=dias), criada_por=victor)
    assuntos = lambda q: [i["assunto"] for i in c.get("/api/todos?" + q).json()["itens"]]  # noqa: E731
    assert assuntos("periodo=fazer") == ["Vencida", "Hoje", "Amanhã", "Mês que vem"]
    assert assuntos("periodo=vencido") == ["Vencida"]
    assert assuntos("periodo=hoje") == ["Hoje"]
    assert assuntos("periodo=amanha") == ["Amanhã"]
    de, ate = hoje + timedelta(days=20), hoje + timedelta(days=40)
    assert assuntos(f"periodo=periodo&de={de}&ate={ate}") == ["Mês que vem"]
    assert assuntos(f"periodo=fazer&responsavel={bruno.id}") == ["Amanhã"]


def test_oportunidades_sem_atividade(cenario, db):  # noqa: F811
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    a, _ = _lead(db, 5), _lead(db, 6)
    a.pipedrive_org_id = 555  # a atividade sem pessoa fica na organização do Pipedrive
    db.commit()
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    pipedrive.criar_atividade(db, client, empresa=a, pessoa_id=None, assunto="Ligar", responsavel=victor,
                              vencimento=date.today(), criada_por=victor)
    nomes = lambda q: {o["empresa"]["nome"] for o in c.get("/api/oportunidades?vertical=saude&" + q).json()["itens"]}  # noqa: E731
    assert nomes("ativ=sem") == {"Lead 6 SA"}
    assert nomes("ativ=com") == {"Lead 5 SA"}
    assert c.get("/api/oportunidades").json()["semAtividade"] >= 1


def test_diario_mostra_campo_a_campo_o_que_a_plataforma_preencheu(cenario, db):  # noqa: F811
    from crosssell.models import AlteracaoDado, Pessoa
    from crosssell.pipeline import registrar

    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = Empresa(razao_social="Leitura SA", nome_normalizado="leitura sa")
    db.add(e)
    db.commit()

    def linkedin_leu(db):  # o que um passo de análise faria
        e.funcionarios, e.setor = 230, "Software"
        db.add(Pessoa(nome="Ana Souza", nome_normalizado="ana souza", empresa=e, cargo="Gerente de RH", fonte="linkedin"))
        db.commit()
        return {"empresas": 1}

    registrar(db, "linkedin", linkedin_leu, db)

    def pipedrive_mudou(db):  # o Pipedrive não entra no diário
        e.cidade = "Recife"
        db.commit()
        return {}

    registrar(db, "pipedrive", pipedrive_mudou, db)
    campos = {(x.campo, x.antes, x.depois, x.fonte) for x in db.scalars(tabela.select(AlteracaoDado))}
    assert campos == {("funcionários", None, "230", "linkedin"), ("setor", None, "Software", "linkedin"),
                      ("pessoa nova", None, "Ana Souza · Gerente de RH", "linkedin")}
    hoje = tabela.hoje_brasilia().isoformat()
    d = c.get("/api/diario").json()["dias"][0]
    assert d["preenchido"] == {"total": 3, "empresas": 1, "porFonte": {"LinkedIn": 3}}
    r = c.get(f"/api/diario/preenchido?data={hoje}").json()
    assert r["total"] == 3 and r["empresas"][0]["nome"] == "Leitura SA"
    assert {i["campo"] for i in r["empresas"][0]["itens"]} == {"funcionários", "setor", "pessoa nova"}
