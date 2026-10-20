from datetime import datetime

from crosssell.models import Empresa
from crosssell.tabela import pesquisa


def _op(vertical, faltando=(), pendencias=(), decide=None):
    return {"vertical": vertical, "faltando": list(faltando), "pendencias": [{"campo": c} for c in pendencias],
            "quemDecide": decide or {"areas": ["RH/Pessoas"], "daArea": True, "pessoas": [{"id": 1}]}}


def test_tudo_feito_e_100():
    p = pesquisa(_op("saude"), Empresa(razao_social="X", linkedin_areas={}))
    assert p["pct"] == 100 and p["estado"] == "completo"
    assert [x["texto"] for x in p["etapas"]] == ["cidade e praça", "funcionários", "setor", "quem decide (RH/Pessoas)",
                                                "ponte por e-mail", "cargo da ponte"]


def test_plataforma_ainda_pesquisa_quem_decide_no_linkedin():
    decide = {"areas": ["Financeiro"], "daArea": False, "pessoas": [{"id": 1}]}
    p = pesquisa(_op("linhas_financeiras", decide=decide), Empresa(razao_social="X", linkedin_areas={}))
    assert p["estado"] == "pesquisando" and p["pct"] == 86  # 6 de 7
    assert next(x for x in p["etapas"] if x["texto"].startswith("quem decide"))["quem"] == "plataforma"


def test_depois_do_linkedin_o_que_falta_e_da_equipe():
    decide = {"areas": ["Financeiro"], "daArea": False, "pessoas": [{"id": 1}]}
    e = Empresa(razao_social="X", linkedin_areas={"pessoas_lf": "2026-10-01T10:00:00"})
    p = pesquisa(_op("linhas_financeiras", decide=decide, pendencias=["cargo"]), e, agora=datetime(2026, 10, 20))
    assert p["estado"] == "equipe" and p["pct"] == 71  # 5 de 7
    assert {x["texto"] for x in p["etapas"] if not x["ok"]} == {"quem decide (Financeiro)", "cargo da ponte"}
    quem = next(x for x in p["etapas"] if x["texto"].startswith("quem decide"))
    assert quem["dica"] == "LinkedIn procurado em 01/10/2026 sem achar ninguém; nova busca em 30/03/2027, ou preencha à mão"
    # Passada a validade, a plataforma volta a procurar sozinha
    depois = pesquisa(_op("linhas_financeiras", decide=decide), e, agora=datetime(2027, 4, 1))
    assert depois["estado"] == "pesquisando"
    # Empresa sem página no LinkedIn
    sem = Empresa(razao_social="X", linkedin_areas={}, linkedin_nao_encontrado=True)
    assert "empresa sem LinkedIn" in next(x for x in pesquisa(_op("linhas_financeiras", decide=decide), sem)["etapas"]
                                          if x["texto"].startswith("quem decide"))["dica"]


def test_sem_ponte_nao_pede_cargo_e_incluida_a_mao_mostra_o_que_falta():
    p = pesquisa(_op("ramos_elementares", faltando=["setor"], pendencias=["ponte"]),
                 Empresa(razao_social="X", linkedin_areas={}))
    assert [x["texto"] for x in p["etapas"] if not x["ok"]] == ["setor", "ponte por e-mail"]
    assert p["pct"] == 50 and p["estado"] == "pesquisando"
