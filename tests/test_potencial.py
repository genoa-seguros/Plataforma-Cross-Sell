"""Critérios de cada vertical (config/criterios.yaml) e Score de Influência."""

from crosssell.models import Empresa, Noticia, Pessoa
from crosssell.potencial import influencia, potencial, produto_lf


def empresa(**kw) -> Empresa:
    # Setor e funcionários de teste vêm do LinkedIn (só esses valem; os do Pipedrive não contam)
    if kw.get("setor"):
        kw.setdefault("setor_fonte", "linkedin")
    if kw.get("funcionarios"):
        kw.setdefault("funcionarios_fonte", "linkedin")
    e = Empresa(razao_social=kw.pop("nome", "Empresa"), nome_normalizado="empresa", **kw)
    e.pessoas, e.noticias, e.negocios = [], [], []
    return e


def pessoa(e: Empresa, nome: str, cargo: str, rel: float = 0, temp: str | None = None) -> Pessoa:
    p = Pessoa(nome=nome, nome_normalizado=nome.lower(), cargo=cargo, score_relacionamento=rel, temperatura=temp)
    e.pessoas.append(p)
    return p


def textos(pot: dict) -> list[str]:
    return [c["texto"] for c in pot["criterios"] if c["texto"]]


def test_influencia_cresce_com_hierarquia_e_relacao():
    e = empresa()
    ceo_proximo = pessoa(e, "Ana", "CEO", rel=80, temp="muita")
    ceo_distante = pessoa(e, "Bia", "CEO", rel=0)
    analista_proximo = pessoa(e, "Caio", "Analista de RH", rel=80)
    assert influencia(ceo_proximo)["score"] > influencia(analista_proximo)["score"]
    assert influencia(ceo_proximo)["score"] > influencia(ceo_distante)["score"]
    assert influencia(ceo_proximo)["score"] == 97.5  # 50% × (0,80 + 0,15) + 50% × 1,0


def test_saude_fintech_na_capital_com_rh_estruturado_vence_industria_do_interior():
    fintech = empresa(nome="PagFácil", setor="Serviços financeiros", descricao="Fintech de pagamentos",
                      funcionarios=400, cidade="São Paulo", uf="SP")
    pessoa(fintech, "Carla", "Head de Pessoas e Cultura")
    cfo = pessoa(fintech, "Davi", "CFO", rel=60)
    industria = empresa(nome="Metalúrgica Beta", cnae="2511000 - Fabricação de estruturas metálicas",
                        funcionarios=400, cidade="Joinville, SC")
    pessoa(industria, "Edu", "Assistente de Departamento Pessoal")
    gerente = pessoa(industria, "Fabi", "CFO", rel=60)

    a, b = potencial("saude", fintech, cfo), potencial("saude", industria, gerente)
    assert a["score"] > b["score"] + 25
    assert "RH estruturado: Carla (Head de Pessoas e Cultura)" in textos(a)
    assert any("time qualificado" in t for t in textos(a))
    assert "Joinville/SC: fora das cidades alvo; falta ver onde estão os funcionários" in textos(b)
    assert any("plano mais simples" in t for t in textos(b))
    assert any(t.startswith("RH só operacional") for t in textos(b))


def test_linhas_financeiras_por_produto():
    assert produto_lf("D&O") == "do" and produto_lf("Cyber") == "cyber" and produto_lf("E&O Medmal") == "eo"
    escritorio = empresa(nome="Souza Advogados", cnae="6911701 - Serviços advocatícios")
    assert any("E&O: presta serviço intelectual" in t for t in textos(potencial("linhas_financeiras", escritorio, None, produto="E&O")))

    familiar = empresa(nome="Padaria Irmãos Silva Ltda", cnae="1091101 - Panificação", funcionarios=40)
    pessoa(familiar, "João Silva", "Sócio-Administrador", rel=50)
    startup = empresa(nome="Dados Seguros", setor="Software", funcionarios=120, investida=True,
                      natureza_juridica="205-4 - Sociedade Anônima Fechada")
    pessoa(startup, "Lia", "CTO")
    startup.noticias.append(Noticia(titulo="Dados Seguros conclui rodada Série A de R$ 40 mi"))
    do_fam = potencial("linhas_financeiras", familiar, familiar.pessoas[0], produto="D&O")
    do_start = potencial("linhas_financeiras", startup, startup.pessoas[0], produto="D&O")
    assert do_start["score"] > do_fam["score"]
    assert "D&O: sem sinal de gestão profissional (pode ser familiar)" in textos(do_fam)
    assert any("recebeu venture capital" in t and "S.A." in t for t in textos(do_start))
    assert "Cyber: tem CTO na empresa" in textos(potencial("linhas_financeiras", startup, None, produto="Cyber"))

    fundo = empresa(nome="Horizonte Gestora de Recursos", cnae="6630400 - Atividades de administração de fundos")
    assert "fundo/gestora: oferecer IMI (D&O + E&O)" in textos(potencial("linhas_financeiras", fundo, None, produto="D&O"))


def test_re_galpao_e_industria_movem_o_ponteiro():
    # Em RE o perfil vem do setor do LinkedIn (o CNAE só vale para as regras de Linhas Financeiras)
    transp = empresa(nome="Rota Sul", setor="Transporte rodoviário de carga", funcionarios=300)
    escritorio = empresa(nome="Consultoria X", setor="Consultoria", funcionarios=30)
    a, b = potencial("ramos_elementares", transp, None), potencial("ramos_elementares", escritorio, None)
    assert a["score"] > b["score"]
    assert any("galpão/indústria/frota" in t for t in textos(a))
    assert "escritório: incêndio/empresarial básico" in textos(b)
