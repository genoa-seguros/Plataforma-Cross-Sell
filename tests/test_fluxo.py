"""Fluxo ponta a ponta: Pipedrive + Zeca + Quiver + e-mail + Receita -> oportunidades."""

from datetime import date, datetime, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from crosssell.connectors import email_m365, enriquecimento, pipedrive, planilhas
from crosssell.models import Empresa, Negocio, Oportunidade, Pessoa
from crosssell.scoring import oportunidades, relacionamento

CNPJ = "14.069.185/0001-03"
HOJE = date(2026, 9, 30)
AGORA = datetime(2026, 9, 30, 12)


def _pipedrive_transport():
    dados = {
        "organizations": [{"id": 2, "name": "Home Agent Teleserviços S.A.", "website": "https://www.homeagent.com.br",
                           "employee_count": 800,
                           "custom_fields": {"4f808fee58c9a509b20237a2ffb8b3f169293b0f": CNPJ}}],
        "persons": [{"id": 10, "name": "Ana Souza", "org_id": 2, "job_title": "Diretora Financeira",
                     "emails": [{"value": "ana@homeagent.com.br", "primary": True}]}],
        # pipeline 31 = Linhas Financeiras (ganho), pipeline 1 = RE (aberto)
        "deals": [{"id": 100, "title": "D&O Home Agent", "pipeline_id": 31, "status": "won", "value": 50000,
                   "org_id": 2, "person_id": 10, "owner_id": 1, "won_time": "2025-12-01 10:00:00"},
                  {"id": 101, "title": "Patrimonial", "pipeline_id": 1, "status": "open", "org_id": 2, "owner_id": 1},
                  {"id": 102, "title": "Sem vertical", "pipeline_id": 999, "status": "open"}],
    }

    def handler(req: httpx.Request):
        recurso = req.url.path.rsplit("/", 1)[-1]
        if recurso == "users":
            return httpx.Response(200, json={"data": [{"id": 1, "email": "rodrigo.pedroni@innoaseguros.com.br"}]})
        return httpx.Response(200, json={"data": dados[recurso], "additional_data": {"next_cursor": None}})

    return httpx.MockTransport(handler)


def _receita_transport():
    def handler(req):
        return httpx.Response(200, json={
            "razao_social": "HOME AGENT TELESERVICOS SA", "porte": "DEMAIS", "cnae_fiscal": 8220200,
            "cnae_fiscal_descricao": "Atividades de teleatendimento", "capital_social": 5_000_000,
            "municipio": "SAO PAULO", "uf": "SP",
            "qsa": [{"nome_socio": "CARLOS ALBERTO MENDES", "qualificacao_socio": "Diretor"}],
        })
    return httpx.MockTransport(handler)


def _carregar(db, settings):
    pipedrive.sincronizar(db, settings, pipedrive.PipedriveClient("x", transport=_pipedrive_transport()))
    zeca_csv = (
        "CNPJ;Razão Social;Operadora;Contrato;Início Vigência;Fim Vigência;Prêmio;Vidas;Status\n"
        f"{CNPJ};Home Agent;Bradesco;Z-1;01/01/2026;01/12/2026;120.000,00;700;Ativo\n"
        "11.444.777/0001-61;Outra Empresa Ltda;Amil;Z-2;01/03/2026;01/03/2027;10.000,00;40;Ativo\n"
    ).encode()
    planilhas.importar_zeca(db, settings, zeca_csv, "zeca.csv")
    quiver_csv = (
        "CPF/CNPJ,Cliente,E-mail,Produto,Seguradora,Apólice,Status\n"
        "529.982.247-25,Carlos Alberto Mendes,carlos@gmail.com,Auto,Porto,Q-1,Vigente\n"
    ).encode()
    planilhas.importar_quiver(db, settings, quiver_csv, "quiver.csv")
    enriquecimento.enriquecer_todas(db, http=httpx.Client(transport=_receita_transport()))

    u = "rodrigo.pedroni@innoaseguros.com.br"
    msgs = []
    for d in range(0, 60, 4):  # conversa recorrente e recíproca com a Ana
        t = (AGORA - timedelta(days=d)).isoformat() + "Z"
        msgs.append({"message_id": f"s{d}", "thread_id": f"t{d}", "data": t, "de": u, "para": ["ana@homeagent.com.br"]})
        msgs.append({"message_id": f"r{d}", "thread_id": f"t{d}", "data": t, "de": "ana@homeagent.com.br", "para": [u]})
    # contato novo no domínio da empresa, descoberto só pelo e-mail
    msgs.append({"message_id": "x1", "thread_id": "tx", "data": AGORA.isoformat(), "de": "rh@homeagent.com.br", "para": [u]})
    email_m365.registrar_mensagens(db, settings, u, msgs)
    relacionamento.calcular(db, agora=AGORA)
    oportunidades.calcular(db, hoje=HOJE)


def test_fluxo_completo(db, settings):
    _carregar(db, settings)

    empresas = db.scalars(select(Empresa)).all()
    assert len(empresas) == 2  # Pipedrive e Zeca unificados pelo CNPJ
    home = db.scalar(select(Empresa).where(Empresa.cnpj == "14069185000103"))
    assert {n.vertical for n in home.negocios if n.vigente} == {"linhas_financeiras", "saude"}
    assert home.porte == "DEMAIS"
    assert not db.scalar(select(Negocio).where(Negocio.id_externo == "102"))

    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@homeagent.com.br"))
    assert ana.ponto_focal and ana.score_relacionamento > 60 and ana.senioridade == "diretor"
    assert db.scalar(select(Pessoa).where(Pessoa.email == "rh@homeagent.com.br")).empresa_id == home.id
    assert home.score_componentes["acesso_decisor"] is True

    ops = {(o.empresa_id, o.pessoa_id, o.vertical_alvo): o for o in db.scalars(select(Oportunidade))}
    # RE tem negócio aberto -> não é oportunidade; LF e Saúde já são clientes.
    assert not any(k[0] == home.id and k[2] in ("ramos_elementares", "saude", "linhas_financeiras") for k in ops)
    # Ana (diretora, ponto focal) -> Linhas Pessoais, apresentada por quem fala com ela.
    op_ana = ops[(home.id, ana.id, "linhas_pessoais")]
    assert op_ana.ponte_email == "rodrigo.pedroni@innoaseguros.com.br"
    # Sócio Carlos já é cliente PF no Quiver -> não duplica oportunidade de LP.
    carlos_socio = db.scalar(select(Pessoa).where(Pessoa.fonte == "receita"))
    assert (home.id, carlos_socio.id, "linhas_pessoais") not in ops

    outra = db.scalar(select(Empresa).where(Empresa.cnpj == "11444777000161"))
    assert (outra.id, None, "ramos_elementares") in ops
    assert (outra.id, None, "linhas_financeiras") in ops


def test_renovacao_proxima_aumenta_momento(db, settings):
    _carregar(db, settings)
    home = db.scalar(select(Empresa).where(Empresa.cnpj == "14069185000103"))
    mom, motivos = oportunidades.momento(home.negocios, HOJE)
    assert mom == 1.0 and any("Saúde" in m for m in motivos)  # Zeca vence em 62 dias


def test_zeca_marca_migracao_como_cancelada(db, settings):
    _carregar(db, settings)
    csv2 = "CNPJ;Razão Social;Operadora;Contrato;Status\n11.444.777/0001-61;Outra;Amil;Z-2;Ativo\n".encode()
    res = planilhas.importar_zeca(db, settings, csv2, "zeca2.csv")
    assert res["canceladas_por_ausencia"] == 1
    assert db.scalar(select(Negocio).where(Negocio.id_externo == "Z-1")).status == "cancelado"


def test_status_trabalhado_sobrevive_ao_recalculo(db, settings):
    _carregar(db, settings)
    o = db.scalars(select(Oportunidade)).first()
    o.status = "em_andamento"
    db.commit()
    oportunidades.calcular(db, hoje=HOJE)
    assert db.get(Oportunidade, o.id).status == "em_andamento"


def test_linkedin_csv(db, settings):
    csv_ = ("First Name,Last Name,Title,Company,Company Website,Person Linkedin Url\n"
            "Paula,Lima,CFO,Home Agent,homeagent.com.br,https://linkedin.com/in/paula\n").encode()
    enriquecimento.importar_linkedin(db, settings, csv_, "sn.csv")
    p = db.scalar(select(Pessoa).where(Pessoa.nome == "Paula Lima"))
    assert p.senioridade == "c_level" and p.empresa.dominio == "homeagent.com.br"


def test_web(db, engine, settings, monkeypatch):
    _carregar(db, settings)
    from sqlalchemy.orm import sessionmaker

    from crosssell.web import app as webapp

    monkeypatch.setattr(webapp, "init_db", lambda: None)
    Local = sessionmaker(bind=engine, expire_on_commit=False)
    webapp.app.dependency_overrides[webapp.get_db] = lambda: Local()
    c = TestClient(webapp.app)
    assert c.get("/").status_code == 200
    home = db.scalar(select(Empresa).where(Empresa.cnpj == "14069185000103"))
    r = c.get(f"/empresas/{home.id}")
    assert r.status_code == 200 and "Ana Souza" in r.text
    api = c.get("/api/oportunidades").json()
    assert api and api[0]["score"] >= api[-1]["score"]
    webapp.app.dependency_overrides.clear()
