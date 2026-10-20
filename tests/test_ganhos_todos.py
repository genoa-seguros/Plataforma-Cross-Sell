"""To-dos: mudar a data da atividade (vai para o Pipedrive). Oportunidades: filtro "Ganhos recentes" (quem fechou
negócio no Pipedrive nos últimos 14 dias, mesmo sem card aberto)."""

from datetime import date, timedelta

from sqlalchemy import select

from crosssell.connectors import pipedrive
from crosssell.models import Empresa, Negocio, Usuario
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


def _empresa(db, nome, *negocios) -> Empresa:
    e = Empresa(razao_social=nome, nome_normalizado=nome.lower())
    db.add(e)
    for i, (status, titulo, ganho_dias) in enumerate(negocios):
        n = Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo=f"{nome}{i}", pipeline_id=1,
                    status=status, titulo=titulo, produto=titulo, responsavel_email="victor.boldrini@innoaseguros.com.br")
        if ganho_dias is not None:
            n.ganho_em, n.fim_vigencia = date.today() - timedelta(days=ganho_dias), date.today() + timedelta(days=300)
        db.add(n)
    db.commit()
    return e


def test_ganhos_recentes_mesmo_sem_card_aberto(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    so_ganho = _empresa(db, "Recem Fechada SA", ("ganho", "D&O", 3))
    _empresa(db, "Antiga SA", ("ganho", "D&O", 20))  # ganho há mais de 14 dias: fora
    _empresa(db, "Com Card SA", ("ganho", "Cyber", 1), ("aberto", "E&O", None), ("ganho", "Garantia", 2),
             ("ganho", "Garantia", 5), ("ganho", "garantia ", 9))

    padrao = c.get("/api/oportunidades").json()
    assert "Recem Fechada SA" not in {o["empresa"]["nome"] for o in padrao["itens"]}  # sem card aberto: só no filtro
    r = c.get("/api/oportunidades?oferta=ganhos").json()
    nomes = [o["empresa"]["nome"] for o in r["itens"]]
    assert set(nomes) == {"Recem Fechada SA", "Com Card SA"} and r["ofertas"]["ganhos"] == len(r["itens"])
    scores = [(o["score"], o["influencia"]) for o in r["itens"]]
    assert scores == sorted(scores, reverse=True)  # pelo Score, como a lista padrão
    item = next(o for o in r["itens"] if o["empresa"]["id"] == so_ganho.id)
    assert item["ganhos"][0]["produto"] == "D&O" and item["ganhos"][0]["donoNome"].startswith("Victor")
    # Vários negócios do mesmo produto viram um só, com a quantidade e o ganho mais recente
    com_card = next(o for o in r["itens"] if o["empresa"]["nome"] == "Com Card SA")
    garantia = next(g for g in com_card["ganhos"] if g["produto"] == "Garantia")
    assert len(com_card["ganhos"]) == 2 and garantia["qtd"] == 3
    assert garantia["em"] == (date.today() - timedelta(days=2)).isoformat()
    assert item["vertical"] != "linhas_financeiras" or item["tipo"] == "mais"  # LF recém-ganha não é cross
    assert any(not o["analisada"] for o in r["itens"])  # mostra também as ainda não analisadas, com o que falta
    # A ficha da empresa mostra as oportunidades de quem só tem o ganho recente
    assert c.get(f"/api/oportunidades?empresa={so_ganho.id}").json()["itens"]


def test_mudar_data_da_atividade_vai_para_o_pipedrive(cenario, db):  # noqa: F811
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    at = pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto="Ligar", vencimento=date.today(),
                                   responsavel=victor, criada_por=victor)
    nova = date.today() + timedelta(days=3)
    r = c.post(f"/api/atividades/{at.id}/data", headers=H, json={"vencimento": nova.isoformat()})
    assert r.json() == {"ok": True, "vencimento": nova.isoformat()}
    assert fake.atualizadas[-1] == (at.pipedrive_id, {"due_date": nova.isoformat()})
    assert c.post("/api/atividades/9999/data", headers=H, json={"vencimento": nova.isoformat()}).status_code == 404


def test_atualizacao_na_atividade_vai_para_a_oportunidade_e_nao_mexe_na_observacao(cenario, db, settings, monkeypatch):  # noqa: F811
    import json

    from crosssell import nota_ia
    from crosssell.models import NotaOportunidade
    from crosssell.web import app as webapp
    from tests.test_fluxo import _ClaudeFalso

    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    falso = _ClaudeFalso(json.dumps({"ajuste": 10, "motivo": "Pediu proposta."}))
    monkeypatch.setattr(webapp, "leitor_notas", lambda db: nota_ia.LeitorNotas(settings, client=falso, modelo="m"))
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    at = pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto="Apresentar Saúde",
                                   vencimento=date.today(), responsavel=victor, criada_por=victor, nota="Levar a proposta")
    escritas = len(fake.atualizadas)
    r = c.post(f"/api/atividades/{at.id}/nota", headers=H, json={"vertical": "saude", "texto": "Apresentou ao RH, pediu proposta"})
    assert r.json()["ajuste"] == 10
    nota = db.scalar(select(NotaOportunidade).where(NotaOportunidade.empresa_id == alfa.id))
    assert nota.vertical == "saude" and nota.texto == "Apresentou ao RH, pediu proposta"
    assert len(fake.atualizadas) == escritas  # a observação da atividade no Pipedrive fica como estava
    item = next(i for i in c.get("/api/todos").json()["itens"] if i["id"] == at.id)
    assert item["nota"] == "Levar a proposta"
    assert c.post(f"/api/atividades/{at.id}/nota", headers=H, json={"vertical": "saude", "texto": "ok"}).status_code == 400


def test_editar_atividade_vai_para_o_pipedrive(cenario, db):  # noqa: F811
    from crosssell.models import Pessoa

    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    bruno = db.scalar(select(Usuario).where(Usuario.email.like("bruno%")))
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    at = pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto="Ligar", vencimento=date.today(),
                                   responsavel=victor, criada_por=victor)
    detalhe = c.get(f"/api/atividades/{at.id}").json()
    assert ana.id in {x["id"] for x in detalhe["contatos"]} and detalhe["pessoaId"] is None
    nova = date.today() + timedelta(days=2)
    r = c.post(f"/api/atividades/{at.id}/editar", headers=H, json={
        "assunto": "Apresentar D&O", "tipo": "meeting", "vencimento": nova.isoformat(), "pessoa_id": ana.id,
        "responsavel_id": bruno.id, "nota": "Levar a proposta", "vertical": "linhas_financeiras"})
    assert r.json() == {"ok": True}
    pid, corpo = fake.atualizadas[-1]
    assert pid == at.pipedrive_id and corpo["subject"] == "Apresentar D&O" and corpo["type"] == "meeting"
    assert corpo["due_date"] == nova.isoformat() and corpo["owner_id"] == 2 and corpo["note"] == "Levar a proposta"
    assert corpo["participants"] == [{"person_id": ana.pipedrive_person_id, "primary": True}]
    item = c.get(f"/api/atividades/{at.id}").json()
    assert item["pessoaId"] == ana.id and item["responsavelId"] == bruno.id and item["assunto"] == "Apresentar D&O"
    assert item["vertical"] == "linhas_financeiras" and item["verticalNome"] == "Linhas Financeiras"
    # Pessoa de outra empresa não vale
    outra = db.scalar(select(Pessoa).where(Pessoa.empresa_id != alfa.id, Pessoa.pipedrive_person_id.is_not(None)))
    r = c.post(f"/api/atividades/{at.id}/editar", headers=H, json={
        "assunto": "X", "tipo": "task", "vencimento": nova.isoformat(), "pessoa_id": outra.id, "responsavel_id": bruno.id})
    assert r.status_code == 400


def test_editar_so_a_oportunidade_nao_escreve_no_pipedrive(cenario, db):  # noqa: F811
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    at = pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto="Ligar", vencimento=date.today(),
                                   responsavel=victor, criada_por=victor)
    escritas = len(fake.atualizadas)
    base = {"assunto": "Ligar", "tipo": "task", "vencimento": date.today().isoformat(), "pessoa_id": None,
            "responsavel_id": victor.id, "nota": ""}
    r = c.post(f"/api/atividades/{at.id}/editar", headers=H, json=base | {"vertical": "ramos_elementares"})
    assert r.json() == {"ok": True} and len(fake.atualizadas) == escritas
    assert c.get(f"/api/atividades/{at.id}").json()["vertical"] == "ramos_elementares"
    # Mudou só a observação: vai só a nota
    c.post(f"/api/atividades/{at.id}/editar", headers=H, json=base | {"nota": "Levar a proposta"})
    assert fake.atualizadas[-1] == (at.pipedrive_id, {"note": "Levar a proposta"})


def test_excluir_atividade_sai_daqui_e_do_pipedrive(cenario, db):  # noqa: F811
    from crosssell.models import Atividade

    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    at = pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto="Ligar", vencimento=date.today(),
                                   responsavel=victor, criada_por=victor)
    aid, pid = at.id, at.pipedrive_id
    assert c.post(f"/api/atividades/{aid}/excluir", headers=H).json() == {"ok": True}
    db.expire_all()  # a API usa outra sessão
    assert pid in fake.excluidas and db.scalar(select(Atividade).where(Atividade.id == aid)) is None
    assert aid not in {i["id"] for i in c.get("/api/todos").json()["itens"]}
    assert c.post(f"/api/atividades/{aid}/excluir", headers=H).status_code == 404
    # Excluída direto no Pipedrive: some daqui na sincronização
    outra = pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto="Reunião",
                                      vencimento=date.today(), responsavel=victor, criada_por=victor)
    fake.excluidas.add(outra.pipedrive_id)
    oid = outra.id
    assert pipedrive.sincronizar_atividades(db, client, date.today() - timedelta(days=7)) == 1
    assert db.get(Atividade, oid) is None
