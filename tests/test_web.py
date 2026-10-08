import re

import httpx
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from crosssell import auth, temperatura
from crosssell.connectors import pipedrive
from crosssell.models import Empresa, Interacao, Negocio, Pessoa, SyncLog, Usuario
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
    webapp.OPORTUNIDADES.limpar()  # as listas guardadas são do processo; cada teste tem seu banco
    webapp.QUALIDADE.limpar()
    yield TestClient(webapp.app), fake
    webapp.app.dependency_overrides.clear()


def mapear_rh(db, e, i=0):
    """Quem decide (RH) e ponte por e-mail: ela escreveu para a equipe e recebeu resposta."""
    rh = Pessoa(nome=f"Rita RH {i}", nome_normalizado=f"rita rh {i}", empresa=e, cargo="Gerente de RH",
                email=f"rita{i}@lead{i}.com.br")
    db.add(rh)
    db.flush()
    for d in ("recebido", "enviado"):
        db.add(Interacao(message_id=f"m{i}{d}", data=datetime.utcnow(), usuario_email="victor.boldrini@innoaseguros.com.br",
                         email_externo=rh.email, direcao=d, pessoa_id=rh.id, empresa_id=e.id))


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


def test_chave_de_leitura_de_emails_na_equipe(cenario, db):
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    r = c.post("/api/equipe/convidar", headers=H, json={"email": "nova@innoaseguros.com.br", "nome": "Nova"}).json()
    uid, token = r["usuario"]["id"], re.search(r"/convite/(.+)$", r["link"]).group(1)
    assert r["usuario"]["leEmails"] is False  # desligada por padrão

    # Convite pendente: a leitura não pode ser ligada
    assert c.post(f"/api/equipe/{uid}/emails", headers=H, json={"ler": True}).status_code == 400

    convidada = TestClient(webapp.app)
    convidada.post(f"/convite/{token}", data={"senha": "senha-nova-123", "confirmacao": "senha-nova-123"})
    assert entrar(convidada, "nova@innoaseguros.com.br", "senha-nova-123").status_code == 303
    assert convidada.post(f"/api/equipe/{uid}/emails", headers=H, json={"ler": True}).status_code == 403  # só master

    u = c.post(f"/api/equipe/{uid}/emails", headers=H, json={"ler": True}).json()
    assert u["leEmails"] is True and u["pendente"] is False
    assert next(x for x in c.get("/api/equipe").json() if x["id"] == uid)["leEmails"] is True

    # Remover o acesso desliga a leitura; convidado de novo, o master liga outra vez
    c.post(f"/api/equipe/{uid}/desconvidar", headers=H)
    db.expire_all()
    assert db.get(Usuario, uid).le_emails is False


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


def test_oportunidades_e_atividade_na_organizacao(cenario, db):
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    # Beta já tem Saúde (marcado no Pipedrive) e negócios abertos em Saúde e LF (Garantia): falta RE.
    # Alfa tem LF vigente e negócios abertos de RE e Saúde: nenhuma oportunidade.
    # RE ainda não tem fluxo de análise: fica fora da tela, mas aparece na ficha da empresa
    assert c.get("/api/oportunidades").json()["itens"] == []
    beta_id = db.scalar(select(Empresa.id).where(Empresa.pipedrive_org_id == 20))
    ops = c.get(f"/api/oportunidades?empresa={beta_id}").json()["itens"]
    assert [(o["empresa"]["nome"], o["vertical"], o["analisada"]) for o in ops] == [("Beta Serviços SA", "ramos_elementares", False)]
    beta = ops[0]
    assert beta["quemDecide"]["areas"] == ["Operações", "Riscos", "Financeiro"]
    victor = next(u for u in c.get("/api/tabela").json()["usuarios"] if u["email"].startswith("victor"))
    r = c.post("/api/atividades", headers=H, json={"empresa_id": beta["empresa"]["id"], "assunto": "Abrir conversa de RE",
                                                    "tipo": "task", "vencimento": "2030-01-04", "responsavel_id": victor["id"]})
    assert r.status_code == 200
    enviada = fake.criadas[-1]
    assert enviada["org_id"] == 20 and "deal_id" not in enviada and "participants" not in enviada
    assert c.get(f"/api/oportunidades?empresa={beta['empresa']['id']}").json()["itens"][0]["proximaAtividade"]["assunto"] \
        == "Abrir conversa de RE"


def test_oportunidades_paginadas_e_filtradas_no_servidor(cenario, db):
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    def lead(i, analisado=True):  # lead negociando LF: oportunidade de Saúde (e de RE, que fica fora da tela)
        e = Empresa(razao_social=f"Lead {i} SA", nome_normalizado=f"lead {i}")
        if analisado:  # praça alvo, funcionários e setor do LinkedIn, RH mapeado e ponte por e-mail: analisada
            e.cidade, e.uf, e.funcionarios, e.setor = "São Paulo", "SP", 120, "Software"
            e.funcionarios_fonte = e.setor_fonte = "linkedin"
        db.add_all([e, Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo=f"8{i}",
                               pipeline_id=1, status="aberto", titulo="D&O")])
        if analisado:
            db.flush()
            mapear_rh(db, e, i)
    for i in range(25):
        lead(i)
    lead(90, analisado=False)  # sem cidade nem funcionários: fica na fila de análise
    db.commit()
    r = c.get("/api/oportunidades").json()
    assert (len(r["itens"]), r["total"], r["totalGeral"], r["pagina"], r["paginas"]) == (20, 25, 25, 1, 2)
    assert r["verticais"] == ["saude"] and r["fila"] == {"saude": 1}
    assert len(c.get("/api/oportunidades?pagina=2").json()["itens"]) == 5
    assert c.get("/api/oportunidades?pagina=99").json()["pagina"] == 2
    assert c.get("/api/oportunidades?vertical=saude").json()["total"] == 25
    assert {o["vertical"] for o in c.get("/api/oportunidades?q=LEAD 7 ").json()["itens"]} == {"saude"}
    assert c.get("/api/oportunidades?tipo=cliente").json()["itens"] == []
    # Toda oportunidade tem ponte por e-mail: ninguém aparece "sem relação com a equipe"
    assert c.get("/api/oportunidades?rel=-").json()["total"] == 0
    assert r["relacoes"] == ["victor.boldrini@innoaseguros.com.br"]
    lead3 = db.scalar(select(Empresa).where(Empresa.razao_social == "Lead 3 SA"))
    assert {o["vertical"] for o in c.get(f"/api/oportunidades?empresa={lead3.id}").json()["itens"]} == \
        {"saude", "ramos_elementares"}  # a ficha mostra todas

    # A lista fica guardada: o que muda direto no banco só aparece quando a plataforma altera algo
    lead(91)
    db.commit()
    assert c.get("/api/oportunidades").json()["totalGeral"] == 25
    assert c.post("/api/melhorias", headers=H, json={"titulo": "Teste"}).status_code == 200
    assert c.get("/api/oportunidades").json()["totalGeral"] == 26
    # ... ou quando a rotina termina um passo (sync_log novo)
    lead(92)
    db.add(SyncLog(fonte="pipedrive", inicio=datetime(2026, 10, 6, 12), fim=datetime(2026, 10, 6, 12, 1), registros=1))
    db.commit()
    assert c.get("/api/oportunidades").json()["totalGeral"] == 27


def test_modelo_da_ia_escolhido_pelo_master(cenario, db):
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    ia = c.get("/api/ia").json()
    assert (ia["modelo"], ia["origem"]) == ("claude-sonnet-5-5", "servidor")  # sem escolha: o do .env/padrão
    assert {o["id"] for o in ia["opcoes"]} == {"claude-sonnet-5-5", "claude-opus-5-5"}
    assert c.post("/api/ia", headers=H, json={"modelo": "claude-haiku-4-5"}).status_code == 400
    r = c.post("/api/ia", headers=H, json={"modelo": "claude-opus-5-5"}).json()
    assert (r["modelo"], r["origem"]) == ("claude-opus-5-5", "plataforma")
    # A rotina usa a escolha da plataforma
    s = webapp.get_settings()
    assert temperatura.modelo_em_uso(db, s) == ("claude-opus-5-5", "plataforma")

    r = c.post("/api/equipe/convidar", headers=H, json={"email": "membro@innoaseguros.com.br", "nome": "Membro"}).json()
    membro = TestClient(webapp.app)
    membro.post(f"/convite/{re.search(r'/convite/(.+)$', r['link']).group(1)}",
                data={"senha": "senha-nova-123", "confirmacao": "senha-nova-123"})
    entrar(membro, "membro@innoaseguros.com.br", "senha-nova-123")
    assert membro.get("/api/ia").status_code == 403
    assert membro.post("/api/ia", headers=H, json={"modelo": "claude-sonnet-5-5"}).status_code == 403


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


def test_qualidade_paginada(cenario, db):
    c, _ = cenario
    for i in range(25):
        db.add(Empresa(razao_social=f"Loja {i}", nome_normalizado=f"loja {i}", pipedrive_org_id=1000 + i,
                       cnpj=f"{i:014d}", razao_receita=f"LOJA {i} COMERCIO LTDA"))
    db.commit()
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    q = c.get("/api/qualidade").json()
    assert "duplicadas" in q and len(q["sugestoes"]) == 20 and q["sugestoesPagina"]["pagina"] == 1
    p2 = c.get("/api/qualidade/sugestoes?pagina=2").json()
    assert "duplicadas" not in p2 and p2["sugestoesPagina"]["pagina"] == 2
    assert len(p2["sugestoes"]) == q["sugestoesPagina"]["total"] - 20


def test_qualidade_duplicadas_paginadas_no_servidor(cenario, db):
    c, _ = cenario
    for i in range(25):  # 25 pares com o mesmo nome no Pipedrive
        for org in (2000 + i, 3000 + i):
            db.add(Empresa(razao_social=f"Duplicada {i} Ltda", nome_normalizado=f"duplicada {i}", pipedrive_org_id=org))
    db.commit()
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    q = c.get("/api/qualidade").json()
    assert len(q["duplicadas"]) == 20 and q["duplicadasPagina"] == {"total": 25, "pagina": 1, "paginas": 2}
    p2 = c.get("/api/qualidade/duplicadas?pagina=2").json()
    assert len(p2["duplicadas"]) == 5 and "sugestoes" not in p2
    # Ignorar pela plataforma renova a lista guardada
    assert c.post("/api/qualidade/ignorar", headers=H, json={"chave": q["duplicadas"][0]["chave"]}).status_code == 200
    assert c.get("/api/qualidade/duplicadas").json()["duplicadasPagina"]["total"] == 24


def test_aba_rotina(cenario, db):
    """A aba Rotina mostra os limites em vigor e a última execução de cada passo, para qualquer usuário."""
    c, _ = cenario
    assert c.get("/api/rotina").status_code == 401
    db.add_all([SyncLog(fonte="pipedrive", inicio=datetime(2026, 10, 6, 13, 0), fim=datetime(2026, 10, 6, 13, 1), registros=42),
                SyncLog(fonte="email", inicio=datetime(2026, 10, 6, 13, 1), erro="1 de 2 caixas não foram lidas")])
    db.commit()
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    r = c.get("/api/rotina").json()
    assert r["minutos"] == 60 and r["dias"] == 2 and r["qualidadeDias"] == 7
    assert r["linkedin"]["validadeDias"] == 180 and r["linkedin"]["limiteDia"] == 50 and r["linkedin"]["buscaDias"] == 30
    assert r["ultimos"]["pipedrive"] == {"em": "2026-10-06T13:00", "registros": 42, "erro": None}
    assert r["ultimos"]["email"]["erro"].startswith("1 de 2") and r["ultimos"]["linkedin"] is None


def test_mesclar_nomes_identicos_pela_api(cenario, db):
    """A Qualidade mostra quantos grupos têm o nome idêntico; só o master dispara a mesclagem em lote."""
    c, _ = cenario
    db.add_all([Empresa(razao_social="Loja Única", nome_normalizado="loja unica", pipedrive_org_id=2001),
                Empresa(razao_social="Loja Única", nome_normalizado="loja unica", pipedrive_org_id=2002, cnpj="11111111000111"),
                Empresa(razao_social="LOJA ÚNICA", nome_normalizado="loja unica", pipedrive_org_id=2003)])
    db.commit()
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    assert c.get("/api/qualidade").json()["nomesIdenticos"] == {"grupos": 1, "organizacoes": 1}

    _, token = auth.convidar(db, "membro@innoaseguros.com.br", "Membro", ["saude"])
    auth.aceitar_convite(db, auth.usuario_do_convite(db, token), "senha-do-membro-1")
    m = TestClient(webapp.app)
    entrar(m, "membro@innoaseguros.com.br", "senha-do-membro-1")
    assert m.post("/api/qualidade/mesclar-nomes-identicos", headers=H, json={}).status_code == 403
    # Essas organizações não existem no Pipedrive (falso): nada é mesclado, o grupo fica para conferir
    r = c.post("/api/qualidade/mesclar-nomes-identicos", headers=H, json={}).json()
    assert r["mescladas"] == 0 and r["conferir"] == 1 and r["restantes"] == 0


def test_suspeitos_e_exclusao_pela_api(cenario, db):
    c, _ = cenario
    db.add(Empresa(razao_social="NAO TENHO", nome_normalizado="nao tenho", pipedrive_org_id=3001))
    db.commit()
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    assert [x["motivoNome"] for x in c.get("/api/qualidade").json()["suspeitas"]] == ["Sem empresa"]
    assert c.post("/api/qualidade/excluir", headers=H, json={"ids": list(range(51))}).status_code == 400

    _, token = auth.convidar(db, "membro@innoaseguros.com.br", "Membro", ["saude"])
    auth.aceitar_convite(db, auth.usuario_do_convite(db, token), "senha-do-membro-1")
    m = TestClient(webapp.app)
    entrar(m, "membro@innoaseguros.com.br", "senha-do-membro-1")
    assert m.post("/api/qualidade/excluir", headers=H, json={"ids": [1]}).status_code == 403


def test_head_acessa_a_qualidade_mas_nao_a_equipe(cenario, db):
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    _, token = auth.convidar(db, "victor.boldrini@innoaseguros.com.br", "Victor Boldrini", ["linhas_financeiras"])
    v = auth.usuario_do_convite(db, token)
    auth.aceitar_convite(db, v, "senha-do-victor-1")
    h = TestClient(webapp.app)
    entrar(h, "victor.boldrini@innoaseguros.com.br", "senha-do-victor-1")
    assert h.get("/api/qualidade").status_code == 403  # membro ainda

    assert c.post(f"/api/equipe/{v.id}/papel", headers=H, json={"papel": "dono"}).status_code == 400
    assert h.post(f"/api/equipe/{v.id}/papel", headers=H, json={"papel": "head"}).status_code == 403  # só o master muda
    assert c.post(f"/api/equipe/{v.id}/papel", headers=H, json={"papel": "head"}).json()["papel"] == "head"
    assert h.get("/api/qualidade").status_code == 200
    assert h.post("/api/qualidade/excluir", headers=H, json={"ids": []}).status_code == 200
    assert h.get("/api/equipe").status_code == 403  # a equipe continua só com o master
    master = db.scalar(select(Usuario).where(Usuario.papel == "master"))
    assert c.post(f"/api/equipe/{master.id}/papel", headers=H, json={"papel": "membro"}).status_code == 400

    assert c.post(f"/api/equipe/{v.id}/papel", headers=H, json={"papel": "membro"}).json()["papel"] == "membro"
    assert h.get("/api/qualidade").status_code == 403


def test_funcionarios_informados_a_mao_valem_por_180_dias(cenario, db):
    from datetime import timedelta
    from crosssell.connectors import linkedin as lk

    c, _ = cenario
    e = db.scalars(select(Empresa)).first()
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    assert c.post(f"/api/empresas/{e.id}/funcionarios", headers=H, json={"funcionarios": 0}).status_code == 400
    r = c.post(f"/api/empresas/{e.id}/funcionarios", headers=H, json={"funcionarios": 250}).json()
    assert r == {"funcionarios": 250, "funcionariosFonte": "manual"}
    db.expire_all()
    e = db.get(Empresa, e.id)
    agora = e.funcionarios_em
    lk._aplicar_empresa(db, e, {"funcionarios": 900}, agora + timedelta(days=30))
    assert (e.funcionarios, e.funcionarios_fonte) == (250, "manual")  # o LinkedIn não passa por cima
    lk._aplicar_empresa(db, e, {"funcionarios": 900}, agora + timedelta(days=181))
    assert (e.funcionarios, e.funcionarios_fonte, e.funcionarios_em) == (900, "linkedin", None)  # depois de 180 dias, atualiza
    db.commit()
    assert c.get(f"/api/empresas/{e.id}").json()["funcionariosFonte"] == "linkedin"


def test_cidade_informada_a_mao_decide_a_praca_e_vale_sobre_a_receita(cenario, db):
    from crosssell.connectors import enriquecimento
    from crosssell.potencial import praca

    c, _ = cenario
    e = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 20))
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    assert c.post(f"/api/empresas/{e.id}/local", headers=H, json={"cidade": "Ribeirão Preto", "uf": "XX"}).status_code == 400
    r = c.post(f"/api/empresas/{e.id}/local", headers=H, json={"cidade": "Ribeirão Preto", "uf": "sp"}).json()
    assert r["local"] == "Ribeirão Preto/SP" and r["cidadeFonte"] == "manual"
    db.expire_all()
    e = db.get(Empresa, e.id)
    assert praca(e) == "fora"  # informada à mão e fora da lista: decide sozinha, sem consulta
    e.cnpj = "14069185000103"

    class Receita(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, json={"razao_social": "BETA SERVICOS SA", "municipio": "SAO PAULO", "uf": "SP"})
    enriquecimento.enriquecer_receita(db, e, httpx.Client(transport=Receita()))
    assert (e.cidade, e.uf, e.cidade_fonte) == ("Ribeirão Preto", "SP", "manual")  # a Receita não passa por cima
    db.commit()
    c.post(f"/api/empresas/{e.id}/local", headers=H, json={"cidade": "", "uf": ""})
    db.expire_all()
    e = db.get(Empresa, e.id)
    enriquecimento.enriquecer_receita(db, e, httpx.Client(transport=Receita()))
    assert (e.cidade, e.cidade_fonte) == ("SAO PAULO", "receita") and praca(e) == "alvo"


def test_cnpj_achado_no_site_so_vale_se_o_nome_bater(db):
    from crosssell.connectors import enriquecimento as en

    e = Empresa(razao_social="Voke Mobilidade", nome_normalizado="voke mobilidade", dominio="voke.com.br")
    outra = Empresa(razao_social="Gama Tech", nome_normalizado="gama tech", dominio="gama.com.br")
    db.add_all([e, outra])
    db.commit()
    paginas = {"voke.com.br": "<footer>Voke Mobilidade Ltda · CNPJ 14.069.185/0001-03 · 11.222.333/0001-99</footer>",
               "gama.com.br": "<footer>Feito por Agência X - CNPJ 14.069.185/0001-03</footer>"}

    class Web(httpx.BaseTransport):
        def handle_request(self, request):
            if "brasilapi" in request.url.host:
                return httpx.Response(200, json={"razao_social": "VOKE MOBILIDADE LTDA", "municipio": "SAO PAULO", "uf": "SP"})
            return httpx.Response(200, text=paginas.get(request.url.host.removeprefix("www."), ""))
    assert en.cnpjs_no_html(paginas["voke.com.br"]) == ["14069185000103"]  # o segundo não tem dígito verificador válido
    res = en.cnpj_por_site(db, [e.id, outra.id], httpx.Client(transport=Web()))
    assert res == {"verificadas": 2, "encontradas": 1}
    assert (e.cnpj, e.cidade, e.cidade_fonte) == ("14069185000103", "SAO PAULO", "receita")
    assert outra.cnpj is None and outra.site_cnpj_em is not None  # CNPJ da agência: nome não bate, não vale


def test_negocios_em_aberto(cenario, db):
    c, _ = cenario
    db.add(Negocio(empresa=db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 20)), vertical=None,
                   fonte="pipedrive", id_externo="777", pipeline_id=39, status="aberto", titulo="Canal parceiro"))
    db.commit()
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    r = c.get("/api/negocios").json()
    assert r["itens"] and set(r["funis"]) <= {"Linhas Financeiras", "RE", "Saúde", "Pipo Saúde"}
    assert not any(n["titulo"] == "Canal parceiro" for n in r["itens"])  # Canais Parceria fica de fora
    funil = r["funis"][0]
    assert all(n["funil"] == funil for n in c.get(f"/api/negocios?funil={funil}").json()["itens"])
    assert c.get("/api/negocios?q=zzzz").json()["total"] == 0


def test_negocios_em_aberto_mostram_o_que_falta_para_oportunidades(cenario, db):
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = Empresa(razao_social="Lead Novo SA", nome_normalizado="lead novo", setor="Fintech", setor_fonte="linkedin")
    db.add_all([e, Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo="991",
                           pipeline_id=1, status="aberto", titulo="D&O")])
    db.commit()
    itens = c.get("/api/negocios").json()["itens"]
    n = next(x for x in itens if x["empresa"] and x["empresa"]["id"] == e.id)
    eid = n["empresa"]["id"]
    assert n["faltaParaOportunidade"] == ["cidade", "funcionários", "quem decide", "ponte por e-mail"]
    assert not n["temOportunidade"]
    assert {"funcionarios", "cidade", "uf", "cidadeFonte"} <= set(n["empresa"])
    # Preencher cidade e funcionários na própria aba leva a empresa para mais perto de Oportunidades
    hdr = {"X-Cross-Sell": "1"}
    assert c.post(f"/api/empresas/{eid}/local", json={"cidade": "São Paulo", "uf": "SP"}, headers=hdr).status_code == 200
    assert c.post(f"/api/empresas/{eid}/funcionarios", json={"funcionarios": 120}, headers=hdr).status_code == 200
    depois = next(x for x in c.get("/api/negocios").json()["itens"] if x["empresa"] and x["empresa"]["id"] == eid)
    assert depois["empresa"]["funcionarios"] == 120 and depois["empresa"]["cidadeFonte"] == "manual"
    # Os dados à mão encurtam a busca, mas sem quem decide e ponte por e-mail ainda não vira oportunidade
    assert depois["faltaParaOportunidade"] == ["quem decide", "ponte por e-mail"] and not depois["temOportunidade"]
    mapear_rh(db, e, 7)
    db.commit()
    c.post(f"/api/empresas/{eid}/setor", json={"setor": "Fintech de pagamentos"}, headers=hdr)  # limpa a lista guardada
    depois = next(x for x in c.get("/api/negocios").json()["itens"] if x["empresa"] and x["empresa"]["id"] == eid)
    assert depois["temOportunidade"] and depois["faltaParaOportunidade"] == []  # agora está em Oportunidades
    assert depois["empresa"]["setor"] == "Fintech de pagamentos"


def test_contato_do_pipedrive_aparece_e_cargo_a_mao(cenario, db):
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = Empresa(razao_social="Pro Eficiencia SA", nome_normalizado="pro eficiencia", pipedrive_org_id=333,
                cidade="Betim", uf="MG", funcionarios=200, setor="Agronegócio")
    natalie = Pessoa(nome="Natalie Barboza", nome_normalizado="natalie barboza", empresa=e, pipedrive_person_id="22312")
    db.add_all([e, natalie, Negocio(empresa=e, pessoa=natalie, vertical="linhas_financeiras", fonte="pipedrive",
                                    id_externo="17004", pipeline_id=1, status="aberto", titulo="D&O 2026", produto="D&O")])
    db.commit()
    op = next(o for o in c.get(f"/api/oportunidades?empresa={e.id}").json()["itens"] if o["vertical"] == "saude")
    # Ninguém de RH e sem e-mails com a equipe: mostra quem temos lá dentro pelo Pipedrive
    contato = op["quemDecide"]["contato"]
    assert contato["nome"] == "Natalie Barboza" and contato["negocio"] == "D&O" and contato["cargo"] is None
    r = c.post(f"/api/pessoas/{natalie.id}/cargo", json={"cargo": "Gerente de RH"}, headers={"X-Cross-Sell": "1"})
    assert r.json() == {"cargo": "Gerente de RH", "noPipedrive": True}
    assert fake.pessoas_atualizadas == [(22312, {"job_title": "Gerente de RH"})]
    op = next(o for o in c.get(f"/api/oportunidades?empresa={e.id}").json()["itens"] if o["vertical"] == "saude")
    assert [p["nome"] for p in op["quemDecide"]["pessoas"]] == ["Natalie Barboza"]  # agora é de RH: quem decide
    assert op["quemDecide"]["daArea"]


def test_fora_da_praca_nao_entra_em_oportunidades_nem_na_fila(cenario, db):
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    e = Empresa(razao_social="Interior SA", nome_normalizado="interior", cidade="Campinas", uf="SP", cidade_fonte="manual",
                funcionarios=500, funcionarios_fonte="linkedin", setor="Software", setor_fonte="linkedin")
    db.add_all([e, Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo="995",
                           pipeline_id=1, status="aberto", titulo="D&O")])
    db.flush()
    mapear_rh(db, e, 5)  # mesmo com quem decide e ponte: cidade à mão fora das cidades alvo decide
    db.commit()
    r = c.get("/api/oportunidades").json()
    assert not any(o["empresa"]["id"] == e.id for o in r["itens"]) and r["fila"] == {"saude": 0}
    ficha = next(o for o in c.get(f"/api/oportunidades?empresa={e.id}").json()["itens"] if o["vertical"] == "saude")
    assert ficha["faltando"] == ["fora da praça"] and ficha["praca"] == "fora"
    n = next(x for x in c.get("/api/negocios").json()["itens"] if x["empresa"] and x["empresa"]["id"] == e.id)
    assert n["faltaParaOportunidade"] == ["fora da praça"]
