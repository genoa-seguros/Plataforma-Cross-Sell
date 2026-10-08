"""Critérios de cada vertical (config/criterios.yaml) e Score de Influência."""

from datetime import datetime

from crosssell.models import Empresa, Noticia, Pessoa
from crosssell.potencial import influencia, potencial, produto_lf, produtos_lf


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
    assert produto_lf("IMI") == "imi"
    escritorio = empresa(nome="Souza Advogados", cnae="6911701 - Serviços advocatícios")
    assert any("E&O: presta serviço intelectual" in t for t in textos(potencial("linhas_financeiras", escritorio, None, produto="E&O")))

    familiar = empresa(nome="Padaria Irmãos Silva Ltda", cnae="1091101 - Panificação", funcionarios=40,
                       noticias_em=datetime(2026, 9, 1))
    pessoa(familiar, "João Silva", "Sócio-Administrador", rel=50)
    startup = empresa(nome="Dados Seguros", setor="Software", funcionarios=120, investida=True,
                      natureza_juridica="205-4 - Sociedade Anônima Fechada", noticias_em=datetime(2026, 9, 1))
    pessoa(startup, "Lia", "CTO")
    startup.noticias.append(Noticia(titulo="Dados Seguros conclui rodada Série A de R$ 40 mi"))
    do_fam = potencial("linhas_financeiras", familiar, familiar.pessoas[0], produto="D&O")
    do_start = potencial("linhas_financeiras", startup, startup.pessoas[0], produto="D&O")
    assert do_start["score"] > do_fam["score"] + 25
    assert "D&O: sem sinal de gestão profissional" in textos(do_fam)
    assert "sem sinal de gestão profissional (pode ser familiar)" in textos(do_fam)
    assert any(t.startswith("D&O: fundos/investidores, S.A.") for t in textos(do_start))
    assert any(t.startswith("profissional: tem fundos/investidores, S.A., diretoria/conselho (CTO)") for t in textos(do_start))
    assert any(t.startswith("M&A/aporte nas notícias: Dados Seguros conclui rodada") for t in textos(do_start))
    assert "Cyber: tem CTO na empresa" in textos(potencial("linhas_financeiras", startup, None, produto="Cyber"))

    # Fundo/gestora: IMI no lugar de D&O e E&O
    fundo = empresa(nome="Horizonte Gestora de Recursos", cnae="6630400 - Atividades de administração de fundos")
    assert [x["produto"] for x in produtos_lf(fundo)] == ["IMI", "Cyber"]
    assert "fundo/gestora de investimentos" in textos(potencial("linhas_financeiras", fundo, None))
    # Sem o que já tem ou negocia
    assert "D&O" not in [x["produto"] for x in produtos_lf(startup, excluir={"do"})]


def test_linhas_financeiras_site_lido_pela_ia_e_portais():
    sem_dado = empresa(nome="Nova Ltda")
    assert textos(potencial("linhas_financeiras", sem_dado, None))[1:3] == [
        "profissionalização: ainda sem Receita, site e LinkedIn", "notícias ainda não buscadas"]

    e = empresa(nome="Arquitetura Viva Ltda", site_ia_em=datetime(2026, 9, 1), noticias_em=datetime(2026, 9, 1),
                site_ia={"fundo_gestora": False, "fundos_investidores": False, "investidores": [],
                         "grandes_clientes": True, "clientes": ["Natura", "Itaú"], "servico_intelectual": True,
                         "site_profissional": True, "servico": "projetos de arquitetura", "resumo": "x"})
    e.noticias.append(Noticia(titulo="Arquitetura Viva assina projeto da nova sede", site="https://braziljournal.com"))
    t = textos(potencial("linhas_financeiras", e, None))
    assert "profissional: grandes clientes (Natura, Itaú), site profissional" in t
    assert "em destaque no Brazil Journal" in t
    assert produtos_lf(e)[0]["texto"] == "E&O: presta serviço intelectual (projetos de arquitetura)"

    e.noticias.insert(0, Noticia(titulo="Arquitetura Viva é adquirida pelo grupo X", site="https://valor.globo.com"))
    assert any(x.startswith("M&A/aporte no Valor Econômico") for x in textos(potencial("linhas_financeiras", e, None)))
    # Site que não abriu não conta como lido pela IA
    e.site_ia = {"erro": "site fora do ar"}
    assert "profissional: grandes clientes" not in " ".join(textos(potencial("linhas_financeiras", e, None)))


def test_re_galpao_e_industria_movem_o_ponteiro():
    # Em RE o perfil vem do setor do LinkedIn (o CNAE só vale para as regras de Linhas Financeiras)
    transp = empresa(nome="Rota Sul", setor="Transporte rodoviário de carga", funcionarios=300)
    escritorio = empresa(nome="Consultoria X", setor="Consultoria", funcionarios=30)
    a, b = potencial("ramos_elementares", transp, None), potencial("ramos_elementares", escritorio, None)
    assert a["score"] > b["score"]
    assert any("galpão/indústria/frota" in t for t in textos(a))
    assert "escritório: incêndio/empresarial básico" in textos(b)
