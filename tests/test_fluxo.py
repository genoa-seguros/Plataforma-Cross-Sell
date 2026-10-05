import os
from datetime import datetime, timedelta

from sqlalchemy import select

from crosssell import tabela
from crosssell.config import Settings
from crosssell.connectors import email_m365, noticias, pipedrive, planilhas
from crosssell.models import Empresa, Negocio, Pessoa, Usuario
from crosssell.pipeline import carregar_usuarios
from crosssell.scoring import relacionamento
from crosssell.temperatura import Classificador, atualizar
from tests.fakes import HOJE, FakePipedrive


def carregar(db, settings, fake=None):
    carregar_usuarios(db, settings)
    fake = fake or FakePipedrive()
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    pipedrive.sincronizar(db, settings, client)
    return client


def neg(db, id_externo):
    return db.scalar(select(Negocio).where(Negocio.fonte == "pipedrive", Negocio.id_externo == str(id_externo)))


def test_regra_de_vigencia(db, settings):
    carregar(db, settings)
    assert neg(db, 1).vigente                      # ganho, fim depois de hoje
    assert not neg(db, 2).vigente                  # ganho, fim antes de hoje (vencido)
    assert neg(db, 2).ex_cliente
    assert not neg(db, 3).vigente                  # ganho sem fim de vigência
    assert neg(db, 10).status == "cancelado" and not neg(db, 10).vigente
    assert neg(db, 1).produto == "D&O" and neg(db, 2).produto == "Cyber"
    assert neg(db, 3).produto == "Garantia"        # sem campo de produto: título sem o ano
    assert neg(db, 4).etapa == "Em Cotação"
    assert neg(db, 9) is None                      # funil 31 não configurado


def test_tabela_so_negocios_abertos_dos_funis_escolhidos(db, settings):
    carregar(db, settings)
    linhas = tabela.montar(db, settings)
    assert {x["pipedriveId"] for x in linhas} == {"4", "5", "11"}  # 40, 38 e 31 ficam de fora
    pipo = next(x for x in linhas if x["pipedriveId"] == "11")
    assert pipo["empresa"]["funcionarios"] == 500 and pipo["empresa"]["funcionariosOrigem"] == "vidas no negócio"
    assert any(m["texto"] == "500 vidas: empresa grande" and m["sinal"] == "+" for m in pipo["motivos"])
    alfa = next(x for x in linhas if x["pipedriveId"] == "4")
    assert [(v["vertical"], v["produto"]) for v in alfa["vigentes"]] == [("linhas_financeiras", "D&O")]
    assert alfa["funil"] == "RE" and alfa["dono"] == "bruno.rodrigues@innoaseguros.com.br"
    # "Por quê" sem redundância: o que já aparece em Seguros vigentes não se repete
    assert not any("cliente" in m["texto"] or "renovação" in m["texto"] for m in alfa["motivos"])
    assert alfa["pessoa"]["nome"] == "Ana Souza" and 0 < alfa["influencia"] <= 100
    beta = next(x for x in linhas if x["pipedriveId"] == "5")
    # "Já possui o seguro saúde na Genoa?" = Sim -> Saúde vigente por marcação
    assert [v["fonte"] for v in beta["vigentes"]] == ["manual"] and beta["saude"]["manual"]


def test_saude_por_planilha_e_checkbox(db, settings):
    carregar(db, settings)
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    pipedrive.marcar_saude(db, alfa.id, True)
    db.commit()
    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "4")
    assert linha["saude"] == {"zeca": False, "manual": True, "pipedrive": None, "desmarcado": False}
    csv = (f"CNPJ;Razão Social;Operadora;Contrato;Fim Vigência;Status\n"
           f"14.069.185/0001-03;Alfa;Amil;Z-1;{(HOJE + timedelta(days=200)):%d/%m/%Y};Ativo\n").encode()
    planilhas.importar_zeca(db, settings, csv, "zeca.csv")
    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "4")
    assert linha["saude"]["zeca"] is True
    pipedrive.marcar_saude(db, alfa.id, False)
    db.commit()
    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "4")
    assert [v["fonte"] for v in linha["vigentes"] if v["vertical"] == "saude"] == ["zeca"]


def test_atividade_dentro_da_pessoa_e_todos(db, settings):
    fake = FakePipedrive()
    client = carregar(db, settings, fake)
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    bruno = db.scalar(select(Usuario).where(Usuario.email.like("bruno%")))
    assert victor.pipedrive_user_id == 1 and bruno.pipedrive_user_id == 2
    n, ana = neg(db, 4), db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    venc = tabela.semana(HOJE)[1]  # sexta da semana de hoje (no fim de semana, a que passou)
    at = pipedrive.criar_atividade(db, client, negocio=n, pessoa_id=ana.id, assunto="Apresentar Empresarial",
                                   vencimento=venc, responsavel=bruno, criada_por=victor, tipo="call")
    enviada = fake.criadas[0]
    assert enviada["participants"] == [{"person_id": 100, "primary": True}]
    assert enviada["owner_id"] == 2 and enviada["deal_id"] == 4 and enviada["org_id"] == 10
    assert enviada["due_date"] == venc.isoformat() and enviada["type"] == "call"

    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "4")
    assert linha["proximaAtividade"]["assunto"] == "Apresentar Empresarial"
    semana = tabela.todos(db, HOJE)
    assert [i["assunto"] for i in semana["itens"]] == ["Apresentar Empresarial"]

    pipedrive.concluir_atividade(db, client, at)
    assert fake.atualizadas == [(at.pipedrive_id, {"done": True})]
    assert tabela.todos(db, HOJE)["itens"][0]["concluida"] is True
    # Reaberta no Pipedrive -> a sincronização traz de volta
    fake.feitas.clear()
    assert pipedrive.sincronizar_atividades(db, client, HOJE - timedelta(days=7)) == 1
    assert tabela.todos(db, HOJE)["itens"][0]["concluida"] is False


def test_atividade_exige_pessoa_no_pipedrive(db, settings):
    client = carregar(db, settings)
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    avulsa = Pessoa(nome="Sem CRM", nome_normalizado="sem crm", fonte="email")
    db.add(avulsa)
    db.commit()
    try:
        pipedrive.criar_atividade(db, client, negocio=neg(db, 4), pessoa_id=avulsa.id, assunto="x",
                                  vencimento=HOJE, responsavel=victor, criada_por=victor)
    except ValueError as exc:
        assert "Pipedrive" in str(exc)
    else:
        raise AssertionError("deveria exigir pessoa do Pipedrive")


class _Resp:
    def __init__(self, texto):
        self.stop_reason = "end_turn"
        self.content = [type("B", (), {"type": "text", "text": texto})()]


class _ClaudeFalso:
    def __init__(self, texto):
        self.chamadas = []
        texto_ = texto
        chamadas = self.chamadas

        class _Msgs:
            def create(self, **kw):
                chamadas.append(kw)
                return _Resp(texto_)

        self.beta = type("Beta", (), {"messages": _Msgs()})()


def test_temperatura_pelos_emails_do_contato(db, settings):
    carregar(db, settings)
    db.add(Empresa(razao_social="Alfa", nome_normalizado="alfa", dominio="alfa.com.br"))
    u = "victor.boldrini@innoaseguros.com.br"
    agora = datetime.utcnow()
    msgs = [
        {"message_id": "a", "thread_id": "t", "data": agora.isoformat(), "de": u, "para": ["ana@alfa.com.br"], "texto": "Oi Ana"},
        {"message_id": "b", "thread_id": "t", "data": agora.isoformat(), "de": "ana@alfa.com.br", "para": [u],
         "texto": "Claro! Te mando a apólice hoje e podemos falar na quinta?"},
    ]
    textos = {}
    email_m365.registrar_mensagens(db, settings, u, msgs, textos)
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    assert list(textos) == [ana.id] and len(textos[ana.id]) == 1  # só o que a Ana escreveu
    falso = _ClaudeFalso('{"temperatura": "muita", "justificativa": "Oferece documentos e propõe reunião."}')
    assert atualizar(db, Classificador(settings, client=falso), textos) == 1
    assert ana.temperatura == "muita" and "reunião" in ana.temperatura_motivo
    chamada = falso.chamadas[0]
    assert chamada["model"] == settings.anthropic_model
    assert chamada["output_config"]["format"]["type"] == "json_schema"
    assert "Oi Ana" not in chamada["messages"][0]["content"]  # e-mail enviado pela equipe não entra
    relacionamento.calcular(db)


def test_chave_da_claude_vem_do_arquivo_env(tmp_path, monkeypatch):
    # O .env é lido pelo Settings, não vira variável de ambiente: a chave precisa chegar ao SDK por ele
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    arquivo = tmp_path / ".env"
    arquivo.write_text("ANTHROPIC_API_KEY=chave-do-arquivo\n", encoding="utf-8")
    assert Classificador(Settings(_env_file=arquivo)).client.api_key == "chave-do-arquivo"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "chave-do-ambiente")  # sem .env: continua valendo o ambiente
    assert Classificador(Settings(_env_file=None)).client.api_key == "chave-do-ambiente"


def test_verticais_yaml_lido_uma_vez_e_relido_quando_muda(tmp_path):
    arquivo = tmp_path / "verticais.yaml"
    arquivo.write_text("empresas_internas: [innoa]\n", encoding="utf-8")
    s = Settings(_env_file=None, verticais_file=arquivo)
    assert s.empresas_internas() == ["innoa"]
    assert s.verticais_config() is s.verticais_config()  # sem reler o arquivo a cada chamada
    arquivo.write_text("empresas_internas: [innoa, genoa]\n", encoding="utf-8")
    os.utime(arquivo, (arquivo.stat().st_atime, arquivo.stat().st_mtime + 5))
    assert s.empresas_internas() == ["innoa", "genoa"]  # editar o arquivo vale sem reiniciar


def test_noticias_rss():
    xml = """<rss><channel>
      <item><title>Metalúrgica Alfa investe R$ 50 mi em nova fábrica - Valor</title><source>Valor</source>
        <link>https://news.example/1</link><pubDate>Mon, 28 Sep 2026 10:00:00 GMT</pubDate></item>
      <item><title>Outra empresa qualquer cresce - Folha</title><source>Folha</source><link>https://news.example/2</link></item>
    </channel></rss>"""
    itens = noticias.ler_rss(xml)
    assert itens[0]["titulo"] == "Metalúrgica Alfa investe R$ 50 mi em nova fábrica" and itens[0]["fonte"] == "Valor"
    assert noticias._cita(itens[0]["titulo"], "metalurgica alfa")
    assert not noticias._cita(itens[1]["titulo"], "metalurgica alfa")


def test_oportunidades_incluem_leads_em_negociacao(db, settings):
    carregar(db, settings)
    lead = Empresa(razao_social="Gama Tech SA", nome_normalizado="gama tech", setor="Software", funcionarios=300,
                   cidade="São Paulo", uf="SP")
    parceiro = Empresa(razao_social="Corretora Parceira", nome_normalizado="corretora parceira")
    db.add_all([lead, parceiro])
    db.flush()
    db.add_all([Negocio(empresa=lead, vertical="linhas_financeiras", fonte="pipedrive", id_externo="900", pipeline_id=1,
                        status="aberto", titulo="D&O 2026", produto="D&O"),
                Negocio(empresa=parceiro, vertical=None, fonte="pipedrive", id_externo="901", pipeline_id=39,
                        status="aberto", titulo="Canal")])
    db.commit()
    ops = {(o["empresa"]["nome"], o["vertical"]): o for o in tabela.oportunidades(db, settings)}
    assert ("Gama Tech SA", "saude") in ops and ("Gama Tech SA", "ramos_elementares") in ops
    saude = ops[("Gama Tech SA", "saude")]
    assert saude["cliente"] is False and [n["produto"] for n in saude["negociando"]] == ["D&O"]
    assert any("time qualificado" in m["texto"] for m in saude["motivos"])
    assert not any(nome == "Corretora Parceira" for nome, _ in ops)  # canal de parceria não é lead de seguro


def test_empresa_interna_fica_fora_e_cancelamento_vira_reconquista(db, settings):
    carregar(db, settings)
    innoa = Empresa(razao_social="Innoa Corretora de Seguros Ltda", nome_normalizado="innoa corretora seguros")
    cli = Empresa(razao_social="Delta Tech", nome_normalizado="delta tech")
    db.add_all([innoa, cli])
    db.flush()
    db.add_all([Negocio(empresa=innoa, vertical="linhas_financeiras", fonte="pipedrive", id_externo="950", pipeline_id=1,
                        status="aberto", titulo="E&O Tecnologia 2025", produto="E&O"),
                Negocio(empresa=cli, vertical="linhas_financeiras", fonte="pipedrive", id_externo="951", pipeline_id=1,
                        status="aberto", titulo="E&O 2026", produto="E&O"),
                Negocio(empresa=cli, vertical="linhas_financeiras", fonte="pipedrive", id_externo="952", pipeline_id=1,
                        status="cancelado", titulo="E&O 2025 Cancelamento", produto="E&O",
                        fim_vigencia=HOJE + timedelta(days=200))])
    db.commit()
    linhas = tabela.montar(db, settings)
    assert not any(x["empresa"] and x["empresa"]["nome"].startswith("Innoa") for x in linhas)
    delta = next(x for x in linhas if x["pipedriveId"] == "951")
    assert any(m["texto"] == "cancelou E&O conosco: reconquista" for m in delta["motivos"])
    assert not any(o["empresa"]["nome"].startswith("Innoa") for o in tabela.oportunidades(db, settings))


def test_saude_ganho_no_pipedrive_vale_ate_desmarcar(db, settings):
    """Saúde não tem fim de vigência: ganho no passado é cliente até a equipe desmarcar."""
    from datetime import date
    carregar(db, settings)
    lead = Empresa(razao_social="Zeta Tech", nome_normalizado="zeta tech")
    db.add(lead)
    db.flush()
    db.add_all([Negocio(empresa=lead, vertical="saude", fonte="pipedrive", id_externo="960", pipeline_id=34,
                        status="ganho", titulo="Saúde 2022", produto="Saúde", ganho_em=date(2022, 3, 10)),
                Negocio(empresa=lead, vertical="linhas_financeiras", fonte="pipedrive", id_externo="961", pipeline_id=1,
                        status="aberto", titulo="D&O 2026", produto="D&O")])
    db.commit()
    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "961")
    assert [(v["vertical"], v["vitalicio"], v["ganhoEm"]) for v in linha["vigentes"]] == [("saude", True, "2022-03-10")]
    assert linha["saude"]["pipedrive"] == "2022-03-10" and not linha["saude"]["desmarcado"]
    assert not any(o["empresa"]["nome"] == "Zeta Tech" and o["vertical"] == "saude" for o in tabela.oportunidades(db, settings))

    pipedrive.marcar_saude(db, lead.id, False)  # "não é mais cliente Saúde"
    db.commit()
    db.expire_all()
    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "961")
    assert linha["vigentes"] == [] and linha["saude"]["desmarcado"]
    op = next(o for o in tabela.oportunidades(db, settings) if o["empresa"]["nome"] == "Zeta Tech" and o["vertical"] == "saude")
    assert any("reconquista" in m["texto"] for m in op["motivos"])

    pipedrive.marcar_saude(db, lead.id, True)  # confirmado: continua conosco, sem duplicar o chip
    db.commit()
    db.expire_all()
    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "961")
    assert [v["fonte"] for v in linha["vigentes"]] == ["pipedrive"] and linha["saude"]["manual"]
