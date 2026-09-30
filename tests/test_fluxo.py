"""Fluxo ponta a ponta: Pipedrive + Zeca + Quiver + e-mail + Receita -> oportunidades."""

from datetime import date, datetime, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from crosssell.connectors import email_m365, enriquecimento, pipedrive, planilhas
from crosssell.models import Empresa, Interacao, Negocio, Oportunidade, Pessoa
from crosssell.scoring import oportunidades, relacionamento

CNPJ = "14.069.185/0001-03"
HOJE = date.today()
AGORA = datetime.combine(HOJE, datetime.min.time()).replace(hour=12)
INI = "4f808fee58c9a509b20237a2ffb8b3f169293b0f"
VIG_INI, VIG_FIM = "3ee3bdd07bab71fba84767ffb7d5d89f49b1f3d3", "0d4a74f324a5c95618a51042c3185da9c8846bc3"


def _d(dias: int) -> str:
    return (HOJE + timedelta(days=dias)).isoformat()


def _br(dias: int) -> str:
    return (HOJE + timedelta(days=dias)).strftime("%d/%m/%Y")


def _pipedrive_transport():
    dados = {
        "organizations": [{"id": 2, "name": "Home Agent Teleserviços S.A.", "website": "https://www.homeagent.com.br",
                           "employee_count": 800,
                           "custom_fields": {INI: CNPJ}},
                          {"id": 3, "name": "Prospect Ltda", "custom_fields": {}},
                          {"id": 4, "name": "Ex Cliente SA", "custom_fields": {INI: "11.222.333/0001-81"}}],
        "persons": [{"id": 10, "name": "Ana Souza", "org_id": 2, "job_title": "Diretora Financeira",
                     "emails": [{"value": "ana@homeagent.com.br", "primary": True}]}],
        # pipeline 40 = Linhas Financeiras (ganho), pipeline 29 = RE (aberto)
        "deals": [{"id": 100, "title": "D&O 2026", "pipeline_id": 40, "status": "won", "value": 50000,
                   "org_id": 2, "person_id": 10, "owner_id": 1, "won_time": _d(-300) + " 10:00:00",
                   "custom_fields": {VIG_INI: _d(-303), VIG_FIM: _d(62)}},
                  {"id": 101, "title": "Patrimonial", "pipeline_id": 29, "status": "open", "org_id": 2, "owner_id": 1},
                  {"id": 102, "title": "Sem vertical", "pipeline_id": 999, "status": "open"},
                  # Org 3: só tem negócio aberto em RE -> prospecção, não é cliente
                  {"id": 103, "title": "Empresarial", "pipeline_id": 29, "status": "open", "org_id": 3},
                  # Org 4: D&O ganho mas vencido + cancelamento "ganho" -> ex-cliente de LF
                  {"id": 104, "title": "D&O 2024", "pipeline_id": 1, "status": "won", "value": 9000, "org_id": 4,
                   "custom_fields": {VIG_INI: _d(-700), VIG_FIM: _d(-335)}},
                  {"id": 105, "title": "Cyber 2025 Cancelamento", "pipeline_id": 1, "status": "won", "value": -500,
                   "org_id": 4, "custom_fields": {VIG_INI: _d(-100), VIG_FIM: _d(265)}}],
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
        f"{CNPJ};Home Agent;Bradesco;Z-1;{_br(-303)};{_br(62)};120.000,00;700;Ativo\n"
        f"11.444.777/0001-61;Outra Empresa Ltda;Amil;Z-2;{_br(-200)};{_br(165)};10.000,00;40;Ativo\n"
        f"11.222.333/0001-81;Ex Cliente SA;Amil;Z-3;{_br(-200)};{_br(165)};5.000,00;20;Ativo\n"
    ).encode()
    planilhas.importar_zeca(db, settings, zeca_csv, "zeca.csv")
    quiver_csv = (
        "CPF/CNPJ,Cliente,E-mail,Produto,Seguradora,Apólice,Status\n"
        "529.982.247-25,Carlos Alberto Mendes,carlos@gmail.com,Auto,Porto,Q-1,Vigente\n"
    ).encode()
    planilhas.importar_quiver(db, settings, quiver_csv, "quiver.csv")
    enriquecimento.enriquecer_todas(db, http=httpx.Client(transport=_receita_transport()))

    u = "pamela.silva@innoaseguros.com.br"
    msgs = []
    for d in range(0, 60, 4):  # conversa recorrente e recíproca com a Ana
        t = (AGORA - timedelta(days=d)).isoformat() + "Z"
        msgs.append({"message_id": f"s{d}", "thread_id": f"t{d}", "data": t, "de": u, "para": ["ana@homeagent.com.br"]})
        msgs.append({"message_id": f"r{d}", "thread_id": f"t{d}", "data": t, "de": "ana@homeagent.com.br", "para": [u]})
    # contato novo no domínio da empresa, descoberto só pelo e-mail
    msgs.append({"message_id": "x1", "thread_id": "tx", "data": AGORA.isoformat(), "de": "rh@homeagent.com.br", "para": [u]})
    email_m365.registrar_mensagens(db, settings, u, msgs)
    relacionamento.calcular(db, agora=AGORA)
    oportunidades.calcular(db, hoje=HOJE, settings=settings)


def test_fluxo_completo(db, settings):
    _carregar(db, settings)

    empresas = db.scalars(select(Empresa)).all()
    assert len(empresas) == 4  # Pipedrive e Zeca unificados pelo CNPJ
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
    assert op_ana.ponte_email == "pamela.silva@innoaseguros.com.br"
    assert op_ana.responsavel_email is None  # Linhas Pessoais ainda sem equipe no config
    # Sócio Carlos já é cliente PF no Quiver -> não duplica oportunidade de LP.
    carlos_socio = db.scalar(select(Pessoa).where(Pessoa.fonte == "receita"))
    assert (home.id, carlos_socio.id, "linhas_pessoais") not in ops

    outra = db.scalar(select(Empresa).where(Empresa.cnpj == "11444777000161"))
    assert ops[(outra.id, None, "ramos_elementares")].responsavel_email == "bruno.rodrigues@innoaseguros.com.br"
    assert ops[(outra.id, None, "linhas_financeiras")].responsavel_email == "victor.boldrini@innoaseguros.com.br"


def test_funil_nao_e_cliente(db, settings):
    _carregar(db, settings)
    prospect = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 3))
    assert not relacionamento.verticais_vigentes(prospect.negocios)
    assert not db.scalars(select(Oportunidade).where(Oportunidade.empresa_id == prospect.id)).all()


def test_vigencia_vencida_e_cancelamento(db, settings):
    _carregar(db, settings)
    ex = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 4))
    assert relacionamento.verticais_vigentes(ex.negocios) == {"saude"}  # só o Zeca está vigente
    assert db.scalar(select(Negocio).where(Negocio.id_externo == "105")).status == "cancelado"
    op = db.scalar(select(Oportunidade).where(Oportunidade.empresa_id == ex.id,
                                              Oportunidade.vertical_alvo == "linhas_financeiras"))
    assert any("reconquista" in m for m in op.motivos)


def test_responsaveis(settings):
    assert settings.responsaveis("linhas_financeiras") == ["victor.boldrini@innoaseguros.com.br"]
    assert settings.responsaveis("ramos_elementares") == ["bruno.rodrigues@innoaseguros.com.br"]
    # Saúde sem líder definido -> equipe inteira
    assert set(settings.responsaveis("saude")) == {"pedro.acciari@innoaseguros.com.br", "pamela.silva@innoaseguros.com.br"}


def test_ponte_da_propria_vertical_conduz_direto(db, settings):
    _carregar(db, settings)
    outra = db.scalar(select(Empresa).where(Empresa.cnpj == "11444777000161"))
    db.add(Interacao(message_id="p1", data=AGORA, usuario_email="pedro.acciari@innoaseguros.com.br",
                     email_externo="fin@outra.com.br", direcao="recebido", empresa_id=outra.id))
    db.commit()
    oportunidades.calcular(db, hoje=HOJE, settings=settings)
    op = db.scalar(select(Oportunidade).where(Oportunidade.empresa_id == outra.id,
                                              Oportunidade.vertical_alvo == "linhas_financeiras"))
    assert op.ponte_email == "pedro.acciari@innoaseguros.com.br"
    assert any("pode conduzir direto" in m for m in op.motivos)


def test_renovacao_proxima_aumenta_momento(db, settings):
    _carregar(db, settings)
    home = db.scalar(select(Empresa).where(Empresa.cnpj == "14069185000103"))
    mom, motivos = oportunidades.momento(home.negocios, HOJE)
    assert mom == 1.0 and any("Saúde" in m for m in motivos)  # Zeca vence em 62 dias


def test_zeca_marca_migracao_como_cancelada(db, settings):
    _carregar(db, settings)
    csv2 = "CNPJ;Razão Social;Operadora;Contrato;Status\n11.444.777/0001-61;Outra;Amil;Z-2;Ativo\n".encode()
    res = planilhas.importar_zeca(db, settings, csv2, "zeca2.csv")
    assert res["canceladas_por_ausencia"] == 2  # Z-1 e Z-3 sumiram do arquivo
    assert db.scalar(select(Negocio).where(Negocio.id_externo == "Z-1")).status == "cancelado"


def test_status_trabalhado_sobrevive_ao_recalculo(db, settings):
    _carregar(db, settings)
    o = db.scalars(select(Oportunidade)).first()
    o.status = "em_andamento"
    db.commit()
    oportunidades.calcular(db, hoje=HOJE, settings=settings)
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
