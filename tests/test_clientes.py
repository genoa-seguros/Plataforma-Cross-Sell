"""Aba Clientes: clientes ativos (seguro vigente) para o time dar o tier de Saúde à mão."""

from datetime import date, timedelta

from sqlalchemy import select

from crosssell.models import Empresa, Negocio, NotaOportunidade, Pessoa, Usuario
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


def _cliente(db, nome, *negocios):
    e = Empresa(razao_social=nome, nome_normalizado=nome.lower(), funcionarios=320, funcionarios_fonte="pipedrive")
    db.add(e)
    for i, (status, produto, fim_dias) in enumerate(negocios):
        db.add(Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo=f"{nome}{i}",
                       pipeline_id=1, status=status, titulo=produto, produto=produto,
                       fim_vigencia=date.today() + timedelta(days=fim_dias) if fim_dias is not None else None))
    db.commit()
    return e


def test_clientes_ativos_com_produtos_sem_repetir_e_tier(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    rodrigo = db.scalar(select(Usuario).where(Usuario.email.like("rodrigo%")))
    ativa = _cliente(db, "Ativa SA", ("ganho", "D&O", 200), ("ganho", "D&O", 30), ("ganho", "Cyber", 90))
    _cliente(db, "Vencida SA", ("ganho", "D&O", -5))  # vigência acabou: não é cliente ativo
    _cliente(db, "Só Aberto SA", ("aberto", "E&O", 200))  # negócio aberto não torna cliente
    p = Pessoa(nome="Carla Dias", nome_normalizado="carla dias", empresa=ativa, cargo="CFO", ponte_manual=True)
    db.add(p)
    db.add(NotaOportunidade(empresa_id=ativa.id, vertical="saude", texto="Plano renova em março", usuario_id=rodrigo.id))
    db.commit()

    r = c.get("/api/clientes").json()
    nomes = {x["nome"] for x in r["itens"]}
    assert "Ativa SA" in nomes and "Vencida SA" not in nomes and "Só Aberto SA" not in nomes
    assert r["tiers"] == ["A+", "A", "B", "C"]
    item = next(x for x in r["itens"] if x["nome"] == "Ativa SA")
    assert [(p["produto"], p["qtd"]) for p in item["produtos"]] == [("Cyber", 1), ("D&O", 2)]  # um chip de D&O
    assert item["funcionarios"] == 320 and item["funcionariosOrigem"] == "no Pipedrive"
    assert [x["nome"] for x in item["contatos"]] == ["Carla Dias"] and item["contatos"][0]["cargo"] == "CFO"
    assert [n["texto"] for n in item["notas"]] == ["Plano renova em março"] and item["tier"] is None

    assert c.post(f"/api/empresas/{ativa.id}/tier", headers=H, json={"tier": "A+"}).json() == {"tier": "A+"}
    item = next(x for x in c.get("/api/clientes").json()["itens"] if x["nome"] == "Ativa SA")
    assert item["tier"] == "A+" and item["tierPor"] == rodrigo.nome
    assert c.post(f"/api/empresas/{ativa.id}/tier", headers=H, json={"tier": "Z"}).status_code == 400
    # A classificação entra no log da semana de quem classificou
    log = c.get("/api/todos/log").json()
    assert any(i["tipo"] == "tier" and i["texto"] == "classificou o tier de Saúde: A+" for i in log["itens"])
    assert c.post(f"/api/empresas/{ativa.id}/tier", headers=H, json={"tier": None}).json() == {"tier": None}


def test_saude_pode_ser_desmarcado_na_aba_clientes(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = _cliente(db, "Com Saude SA", ("ganho", "D&O", 100))
    db.add(Negocio(empresa=e, vertical="saude", fonte="pipedrive", id_externo="cs-saude", pipeline_id=2, status="ganho",
                   titulo="Saúde", produto="Saúde", ganho_em=date.today() - timedelta(days=200)))
    db.commit()
    item = next(x for x in c.get("/api/clientes").json()["itens"] if x["nome"] == "Com Saude SA")
    saude = next(p for p in item["produtos"] if p["vertical"] == "saude")
    assert saude["desmarcavel"] and not next(p for p in item["produtos"] if p["produto"] == "D&O")["desmarcavel"]
    c.post(f"/api/empresas/{e.id}/saude", headers=H, json={"cliente": False})
    item = next(x for x in c.get("/api/clientes").json()["itens"] if x["nome"] == "Com Saude SA")
    assert [p["produto"] for p in item["produtos"]] == ["D&O"]


def test_clientes_mostra_os_criterios_de_saude_e_o_preenchimento_a_mao_muda_o_score(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = _cliente(db, "Criterios SA", ("ganho", "D&O", 100))
    item = lambda: next(x for x in c.get("/api/clientes").json()["itens"] if x["nome"] == "Criterios SA")  # noqa: E731
    antes = item()
    assert set(antes["saude"]["criterios"]) == {"localizacao", "qualificacao", "funcionarios", "rh"}
    assert antes["rh"] == [] and antes["setor"] is None
    c.post(f"/api/empresas/{e.id}/local", headers=H, json={"cidade": "São Paulo", "uf": "SP"})
    c.post(f"/api/empresas/{e.id}/setor", headers=H, json={"setor": "Software"})
    c.post(f"/api/empresas/{e.id}/funcionarios", headers=H, json={"funcionarios": 800})
    c.post(f"/api/empresas/{e.id}/pessoas", headers=H, json={"nome": "Paula RH", "cargo": "Diretora de RH"})
    depois = item()
    crit = depois["saude"]["criterios"]
    assert crit["localizacao"]["valor"] == 1.0 and crit["qualificacao"]["valor"] == 1.0 and crit["rh"]["valor"] == 1.0
    assert depois["setor"] == "Software" and depois["rh"][0]["nome"] == "Paula RH"
    assert depois["saude"]["score"] > antes["saude"]["score"]
