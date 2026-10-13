"""Ramos Elementares por produto: Fiança (escritório com 50+ funcionários), Patrimonial/Roubo (grandes ativos) e
Transportes; quem decide pelo produto (a ponte já serve se for da área) e cota do LinkedIn Fiança/Patrimonial."""

from datetime import datetime

from crosssell.connectors import linkedin as lk
from crosssell.models import Empresa, Interacao, Negocio, Pessoa
from tests.test_fluxo import carregar
from tests.test_linkedapi import config
from tests.test_web import cenario, entrar  # noqa: F401 (fixture)


def _lead(db, nome, i, setor, funcionarios, cargo_ponte):
    """Lead negociando Saúde, com setor e funcionários do LinkedIn e uma ponte por e-mail com o cargo dado."""
    e = Empresa(razao_social=nome, nome_normalizado=nome.lower(), setor=setor, setor_fonte="linkedin",
                funcionarios=funcionarios, funcionarios_fonte="linkedin")
    ponte = Pessoa(nome=f"Paulo Ponte {i}", nome_normalizado=f"paulo ponte {i}", empresa=e, cargo=cargo_ponte,
                   email=f"paulo@re{i}.com.br")
    db.add_all([e, ponte, Negocio(empresa=e, vertical="saude", fonte="pipedrive", id_externo=f"re{i}", pipeline_id=23,
                                  status="aberto", titulo="Saúde")])
    db.flush()
    for d in ("recebido", "enviado"):
        db.add(Interacao(message_id=f"re{i}{d}", data=datetime.utcnow(), usuario_email="victor.boldrini@innoaseguros.com.br",
                         email_externo=ponte.email, direcao=d, pessoa_id=ponte.id, empresa_id=e.id))
    return e


def test_re_entra_em_oportunidades_quando_a_ponte_e_da_area_do_produto(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    galpao = _lead(db, "Voke Distribuidora SA", 1, "Distribuidora de hardware com galpão e estoque", 80,
                   "Gerente de Compras")
    escritorio = _lead(db, "Consultoria Grande SA", 2, "Consultoria empresarial", 300, "Gerente de Marketing")
    db.commit()

    ops = {o["empresa"]["nome"]: o for o in c.get("/api/oportunidades?vertical=ramos_elementares").json()["itens"]}
    # Patrimonial: a ponte é de Compras, então ela mesma é quem decide
    voke = ops["Voke Distribuidora SA"]
    assert [x["produto"] for x in voke["produtos"]] == ["Patrimonial/Roubo"] and voke["tipo"] == "cross"
    assert voke["quemDecide"]["pessoas"][0]["nome"] == "Paulo Ponte 1"
    # Fiança: a ponte é de Marketing; falta mapear alguém do Financeiro ou Jurídico
    assert "Consultoria Grande SA" not in ops
    ficha = next(o for o in c.get(f"/api/oportunidades?empresa={escritorio.id}").json()["itens"]
                 if o["vertical"] == "ramos_elementares")
    assert [x["produto"] for x in ficha["produtos"]] == ["Fiança"]
    assert ficha["quemDecide"]["areas"] == ["Financeiro", "Jurídico"] and ficha["faltando"] == ["quem decide"]

    # Escritório pequeno (menos de 50 funcionários): nenhum produto de RE faz sentido, não entra (nem na fila)
    pequena = _lead(db, "Agência Pequena Ltda", 4, "Agência de marketing", 20, "CFO")
    db.commit()
    c.post("/api/melhorias", headers={"X-Cross-Sell": "1"}, json={"titulo": "limpa a lista guardada"})
    ficha = next(o for o in c.get(f"/api/oportunidades?empresa={pequena.id}").json()["itens"]
                 if o["vertical"] == "ramos_elementares")
    assert ficha["faltando"] == ["sem produto de RE"]

    # Sem setor ainda: fila de análise
    sem = _lead(db, "Sem Setor SA", 3, None, None, "CFO")
    sem.setor_fonte = sem.funcionarios_fonte = None
    db.commit()
    c.post("/api/melhorias", headers={"X-Cross-Sell": "1"}, json={"titulo": "limpa a lista guardada"})
    ficha = next(o for o in c.get(f"/api/oportunidades?empresa={sem.id}").json()["itens"]
                 if o["vertical"] == "ramos_elementares")
    assert ficha["faltando"][:2] == ["setor", "funcionários"]


def test_linkedin_re_cota_fianca_e_patrimonial(db, settings):
    s = config(settings, linkedin_sales_navigator=True)
    assert lk.cotas(s)[("ramos_elementares", "fianca")] == 5 and lk.cotas(s)[("ramos_elementares", "patrimonial")] == 5
    carregar(db, s)
    agora = datetime.utcnow()

    def empresa(nome, setor, func, cargo=None):
        e = Empresa(razao_social=nome, nome_normalizado=nome.lower(), setor=setor, setor_fonte="linkedin",
                    funcionarios=func, funcionarios_fonte="linkedin", linkedin_url=f"https://www.linkedin.com/company/{func}",
                    linkedin_em=agora, linkedin_areas={"urn": f"urn:li:fsd_company:{func}"})
        db.add_all([e, Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo=f"lf{func}",
                               pipeline_id=1, status="aberto", titulo="D&O")])
        if cargo:
            db.add(Pessoa(nome=f"Fulano {func}", nome_normalizado=f"fulano {func}", empresa=e, cargo=cargo))
        return e

    fianca = empresa("Escritório Grande SA", "Consultoria", 301)
    fabrica = empresa("Fábrica SA", "Indústria de alimentos", 302)
    conhecida = empresa("Usina SA", "Usina de geração de energia", 303, cargo="Gerente de Operações")
    db.commit()
    re = {a["id_alvo"]: a for a in lk.alvos(db, s) if a["vertical"] == "ramos_elementares"}
    assert (re[f"E{fianca.id}"]["grupo"], re[f"E{fianca.id}"]["acao"]) == ("fianca", "pessoas")
    assert "cfo" in re[f"E{fianca.id}"]["cargos"] and "compras" not in re[f"E{fianca.id}"]["cargos"]
    assert re[f"E{fabrica.id}"]["grupo"] == "patrimonial" and "facilities" in re[f"E{fabrica.id}"]["cargos"]
    # Quem decide já conhecido (Operações): não gasta consulta
    assert f"E{conhecida.id}" not in re
