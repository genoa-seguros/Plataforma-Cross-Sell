from datetime import date

from crosssell.normalize import (
    classificar_senioridade, dominio_email, dominio_site, normalizar_cnpj, normalizar_cpf,
    normalizar_nome_empresa, parse_data, parse_numero,
)


def test_cnpj():
    assert normalizar_cnpj("14.069.185/0001-03") == "14069185000103"
    assert normalizar_cnpj("14.069.185/0001-04") is None
    assert normalizar_cnpj("4069185000103") is None  # não é zero à esquerda válido
    assert normalizar_cnpj("11.444.777/0001-61") == "11444777000161"


def test_cpf():
    assert normalizar_cpf("529.982.247-25") == "52998224725"
    assert normalizar_cpf("111.111.111-11") is None


def test_nomes_e_dominios():
    assert normalizar_nome_empresa("Home Agent Teleserviços S.A.") == normalizar_nome_empresa("HOME AGENT TELESERVICOS SA")
    assert dominio_email("Ana@HomeAgent.com.br") == "homeagent.com.br"
    assert dominio_email("ana@gmail.com") is None
    assert dominio_site("https://www.homeagent.com.br/") == "homeagent.com.br"


def test_senioridade():
    assert classificar_senioridade("Diretora Financeira") == "diretor"
    assert classificar_senioridade("CFO") == "c_level"
    assert classificar_senioridade("Sócio-Administrador") == "socio"
    assert classificar_senioridade("Analista de RH") == "outro"


def test_parse():
    assert parse_data("31/12/2026") == date(2026, 12, 31)
    assert parse_numero("R$ 1.234,56") == 1234.56
