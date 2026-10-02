import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from crosssell import auth
from crosssell.connectors import pipedrive
from crosssell.models import Usuario
from crosssell.web import app as webapp
from tests.fakes import FakePipedrive
from tests.test_fluxo import carregar

H = {"X-Cross-Sell": "1"}


@pytest.fixture
def cenario(engine, db, settings, monkeypatch):
    fake = FakePipedrive()
    carregar(db, settings, fake)
    master, _ = auth.convidar(db, "rodrigo.pedroni@innoaseguros.com.br", "Rodrigo", [], papel="master")
    auth.aceitar_convite(db, master, "senha-do-master-123")
    Local = sessionmaker(bind=engine, expire_on_commit=False)
    webapp.app.dependency_overrides[webapp.get_db] = lambda: Local()
    webapp.app.dependency_overrides[webapp.get_pipedrive] = lambda: pipedrive.PipedriveClient("x", transport=fake.transport())
    monkeypatch.setattr(webapp, "get_settings", lambda: settings.model_copy(update={"cookie_seguro": False}))
    yield TestClient(webapp.app), fake
    webapp.app.dependency_overrides.clear()


def entrar(c, email, senha):
    return c.post("/login", data={"email": email, "senha": senha}, follow_redirects=False)


def test_login_obrigatorio_e_senha_errada(cenario):
    c, _ = cenario
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/api/tabela").status_code == 401
    assert entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "errada").status_code == 401
    assert entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123").status_code == 303
    assert c.get("/").status_code == 200
    assert len(c.get("/api/tabela").json()["linhas"]) == 3


def test_convite_desconvite_e_permissoes(cenario, db):
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    assert c.post("/api/equipe/convidar", json={"email": "x@innoaseguros.com.br", "nome": "X"}).status_code == 403  # sem cabeçalho
    r = c.post("/api/equipe/convidar", headers=H, json={"email": "nova@innoaseguros.com.br", "nome": "Nova",
                                                        "verticais": ["saude"], "lider": ["saude"]}).json()
    token = re.search(r"/convite/(.+)$", r["link"]).group(1)

    convidada = TestClient(webapp.app)
    assert convidada.post(f"/convite/{token}", data={"senha": "curta", "confirmacao": "curta"}).status_code == 400
    assert convidada.post(f"/convite/{token}", data={"senha": "senha-nova-123", "confirmacao": "senha-nova-123"},
                          follow_redirects=False).status_code == 303
    assert convidada.get(f"/convite/{token}").status_code == 404  # uso único
    assert entrar(convidada, "nova@innoaseguros.com.br", "senha-nova-123").status_code == 303
    assert convidada.get("/api/equipe").status_code == 403  # membro não gerencia equipe

    uid = r["usuario"]["id"]
    assert c.post(f"/api/equipe/{uid}/desconvidar", headers=H).json()["ativo"] is False
    assert convidada.get("/api/tabela").status_code == 401  # sessão encerrada
    db.expire_all()
    ativos = [u.email for u in db.scalars(select(Usuario).where(Usuario.ativo.is_(True)))]
    assert "nova@innoaseguros.com.br" not in ativos  # e-mails deixam de ser lidos


def test_criar_atividade_e_marcar_saude_pela_api(cenario):
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    t = c.get("/api/tabela").json()
    linha = next(x for x in t["linhas"] if x["pipedriveId"] == "4")
    bruno = next(u for u in t["usuarios"] if u["email"].startswith("bruno"))
    r = c.post("/api/atividades", headers=H, json={"negocio_id": linha["id"], "pessoa_id": linha["contatos"][0]["id"],
                                                    "assunto": "Ligar", "tipo": "call", "vencimento": "2030-01-04",
                                                    "responsavel_id": bruno["id"]})
    assert r.status_code == 200 and fake.criadas[0]["owner_id"] == 2
    todos = c.get("/api/todos?ref=2030-01-04").json()
    assert todos["inicio"] == "2029-12-31" and todos["fim"] == "2030-01-04"
    assert [i["assunto"] for i in todos["itens"]] == ["Ligar"]

    eid = linha["empresa"]["id"]
    assert c.post(f"/api/empresas/{eid}/saude", headers=H, json={"cliente": True}).status_code == 200
    linha = next(x for x in c.get("/api/tabela").json()["linhas"] if x["pipedriveId"] == "4")
    assert linha["saude"]["manual"] is True


def test_oportunidades_e_atividade_na_organizacao(cenario):
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    ops = c.get("/api/oportunidades").json()["itens"]
    # Beta já tem Saúde (marcado no Pipedrive) e negócios abertos em Saúde e LF (Garantia): falta RE.
    # Alfa tem LF vigente e negócios abertos de RE e Saúde: nenhuma oportunidade.
    assert [(o["empresa"]["nome"], o["vertical"]) for o in ops] == [("Beta Serviços SA", "ramos_elementares")]
    beta = ops[0]
    assert beta["quemDecide"]["areas"] == ["Operações", "Riscos", "Financeiro"]
    victor = next(u for u in c.get("/api/tabela").json()["usuarios"] if u["email"].startswith("victor"))
    r = c.post("/api/atividades", headers=H, json={"empresa_id": beta["empresa"]["id"], "assunto": "Abrir conversa de RE",
                                                    "tipo": "task", "vencimento": "2030-01-04", "responsavel_id": victor["id"]})
    assert r.status_code == 200
    enviada = fake.criadas[-1]
    assert enviada["org_id"] == 20 and "deal_id" not in enviada and "participants" not in enviada
    assert c.get("/api/oportunidades").json()["itens"][0]["proximaAtividade"]["assunto"] == "Abrir conversa de RE"
