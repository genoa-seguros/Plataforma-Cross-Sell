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


def test_base_traz_equipe_e_verticais_sem_a_tabela(cenario):
    c, _ = cenario
    assert c.get("/api/base").status_code == 401
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    base = c.get("/api/base").json()
    assert "linhas" not in base and base["verticais"]["saude"] == "Saúde"
    assert "rodrigo.pedroni@innoaseguros.com.br" in {u["email"] for u in base["usuarios"]}


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


def test_esqueci_a_senha(cenario, db, monkeypatch):
    c, _ = cenario
    enviados = []
    monkeypatch.setattr(webapp, "_enviar_link_senha", lambda req, u, token: enviados.append((u.email, token)) or True)
    # Resposta igual para e-mail com e sem acesso
    r1 = c.post("/esqueci", data={"email": "ninguem@innoaseguros.com.br"})
    r2 = c.post("/esqueci", data={"email": "rodrigo.pedroni@innoaseguros.com.br"})
    assert r1.status_code == r2.status_code == 200 and "enviamos um link" in r1.text and "enviamos um link" in r2.text
    assert [e for e, _ in enviados] == ["rodrigo.pedroni@innoaseguros.com.br"]
    c.post("/esqueci", data={"email": "rodrigo.pedroni@innoaseguros.com.br"})
    assert len(enviados) == 1  # pedido repetido em poucos minutos não gera outro link

    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    outra = TestClient(webapp.app)
    token = enviados[0][1]
    assert outra.post(f"/redefinir/{token}", data={"senha": "nova-senha-456", "confirmacao": "outra"}).status_code == 400
    r = outra.post(f"/redefinir/{token}", data={"senha": "nova-senha-456", "confirmacao": "nova-senha-456"})
    assert r.status_code == 200 and "Senha alterada" in r.text
    assert outra.get(f"/redefinir/{token}").status_code == 404  # uso único
    assert c.get("/api/tabela").status_code == 401  # sessões antigas encerradas
    assert entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123").status_code == 401
    assert entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "nova-senha-456").status_code == 303


def test_melhorias(cenario, db):
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    assert c.post("/api/melhorias", headers=H, json={"titulo": " "}).status_code == 400
    x = c.post("/api/melhorias", headers=H, json={"titulo": "Mostrar valor da apólice", "prioridade": "alta"}).json()
    assert x["situacao"] == "nova" and x["autor"] == "Rodrigo"
    # Membro anota, mas não muda a situação
    _, token = auth.convidar(db, "membro@innoaseguros.com.br", "Membro", ["saude"])
    u = auth.usuario_do_convite(db, token)
    auth.aceitar_convite(db, u, "senha-do-membro-1")
    m = TestClient(webapp.app)
    entrar(m, "membro@innoaseguros.com.br", "senha-do-membro-1")
    y = m.post("/api/melhorias", headers=H, json={"titulo": "Filtro por seguradora"}).json()
    assert m.post(f"/api/melhorias/{y['id']}", headers=H, json={"situacao": "feita"}).status_code == 403
    assert m.post(f"/api/melhorias/{x['id']}", headers=H, json={"prioridade": "baixa"}).status_code == 403
    assert c.post(f"/api/melhorias/{y['id']}", headers=H, json={"situacao": "andamento"}).json()["situacao"] == "andamento"
    assert [i["titulo"] for i in m.get("/api/melhorias").json()] == ["Filtro por seguradora", "Mostrar valor da apólice"]


def test_rotina_pulada_quando_outra_esta_rodando(monkeypatch):
    from contextlib import contextmanager

    from crosssell import cli, db as banco

    rodadas = []
    monkeypatch.setattr(cli, "_rodar_rotina", lambda dias: rodadas.append(dias))

    @contextmanager
    def ocupada(nome, bind=None):
        yield False

    monkeypatch.setattr(banco, "trava", ocupada)
    cli.rotina(dias=2)
    assert rodadas == []


def test_trava_nao_bloqueia_no_sqlite(engine):
    from crosssell.db import trava

    with trava("x", bind=engine) as a, trava("x", bind=engine) as b:
        assert a and b  # SQLite é só desenvolvimento: um processo, sem rotina interna


def test_rotina_interna_encerra_rodada_travada(monkeypatch):
    import subprocess

    chamadas = []

    def demorou(cmd, check, timeout):
        chamadas.append(timeout)
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", demorou)

    class Evento:  # espera inicial, depois duas rodadas e para
        def __init__(self):
            self.vezes = 0

        def wait(self, segundos):
            self.vezes += 1
            return self.vezes > 2

    webapp._rotina_interna(Evento(), minutos=60, limite_minutos=90)
    assert chamadas == [90 * 60, 90 * 60]  # a rodada travada não impede a seguinte
