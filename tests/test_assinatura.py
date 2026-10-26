"""Assinatura dos e-mails lida pela IA: cargo, nome completo e telefone de quem escreve à equipe."""

import json
from datetime import datetime

from sqlalchemy import select

from crosssell import assinatura, tabela
from crosssell.connectors import email_m365
from crosssell.models import Empresa, Pessoa
from tests.test_fluxo import _ClaudeFalso, carregar


def test_assinatura_preenche_cargo_nome_e_telefone(db, settings):
    carregar(db, settings)
    db.add(Empresa(razao_social="Alfa", nome_normalizado="alfa", dominio="alfa.com.br"))
    u = "victor.boldrini@innoaseguros.com.br"
    agora = datetime.utcnow()
    texto = "Combinado, envio amanhã.\n\nAtenciosamente,\nJoana Prado\nGerente de Recursos Humanos\n(11) 98888-7777"
    msgs = [{"message_id": "m1", "thread_id": "t", "data": agora.isoformat(), "de": "joana.prado@alfa.com.br",
             "para": [u], "texto": texto}]
    textos = {}
    email_m365.registrar_mensagens(db, settings, u, msgs, textos)
    joana = db.scalar(select(Pessoa).where(Pessoa.email == "joana.prado@alfa.com.br"))
    assert joana.nome == "joana.prado" and joana.cargo is None  # criada pelo e-mail, sem cargo

    falso = _ClaudeFalso(json.dumps({"nome": "Joana Prado", "cargo": "Gerente de Recursos Humanos",
                                     "telefone": "(11) 98888-7777"}))
    leitor = assinatura.LeitorAssinatura(settings, client=falso, modelo="m")
    assert assinatura.atualizar(db, leitor, textos) == 1
    assert (joana.nome, joana.cargo, joana.telefone) == ("Joana Prado", "Gerente de Recursos Humanos", "(11) 98888-7777")
    assert joana.assinatura_em is not None and tabela.area_pessoa(joana) == "rh"  # com cargo, ganha área (quem decide)
    enviado = falso.chamadas[0]["messages"][0]["content"]
    assert "Gerente de Recursos Humanos" in enviado and "joana.prado@alfa.com.br" in enviado
    # Lida uma vez: um e-mail novo não gera outra leitura
    assert assinatura.atualizar(db, leitor, textos) == 0 and len(falso.chamadas) == 1


def test_assinatura_nunca_sobrescreve(db, settings):
    e = Empresa(razao_social="Beta", nome_normalizado="beta")
    db.add(e)
    db.flush()
    com_cargo = Pessoa(nome="Caio Lima", nome_normalizado="caio lima", email="caio@beta.com.br", cargo="CFO", empresa_id=e.id)
    sem_cargo = Pessoa(nome="Rita Souza", nome_normalizado="rita souza", email="rita@beta.com.br", telefone="1133334444",
                       empresa_id=e.id)
    db.add_all([com_cargo, sem_cargo])
    db.commit()
    agora = datetime.utcnow()
    textos = {com_cargo.id: [{"data": agora, "texto": "x"}], sem_cargo.id: [{"data": agora, "texto": "Rita S.\nCoordenadora"}]}
    falso = _ClaudeFalso(json.dumps({"nome": "Rita de Souza", "cargo": "Coordenadora Financeira", "telefone": "(11) 2222-3333"}))
    assert assinatura.atualizar(db, assinatura.LeitorAssinatura(settings, client=falso, modelo="m"), textos) == 1
    assert len(falso.chamadas) == 1  # quem já tem cargo nem é lido
    assert (sem_cargo.nome, sem_cargo.cargo, sem_cargo.telefone) == ("Rita Souza", "Coordenadora Financeira", "1133334444")
    assert com_cargo.cargo == "CFO" and com_cargo.assinatura_em is None


def test_historico_le_a_assinatura_de_quem_esta_em_oportunidade(db, settings):
    """Leitura única dos 12 meses: relê as caixas e lê a assinatura das pessoas sem cargo das empresas pedidas;
    quem tem cargo, já foi lido ou é de outra empresa fica de fora."""
    from crosssell.models import Usuario

    alfa, outra = Empresa(razao_social="Alfa", nome_normalizado="alfa"), Empresa(razao_social="Outra", nome_normalizado="outra")
    db.add_all([alfa, outra])
    db.flush()
    db.add_all([Usuario(email="victor@innoaseguros.com.br", nome="Victor", senha_hash="x", le_emails=True),
                Pessoa(nome="ana", nome_normalizado="ana", email="ana@alfa.com.br", empresa_id=alfa.id),
                Pessoa(nome="Bia Reis", nome_normalizado="bia reis", email="bia@alfa.com.br", empresa_id=alfa.id, cargo="CEO"),
                Pessoa(nome="Zeca", nome_normalizado="zeca", email="zeca@outra.com.br", empresa_id=outra.id)])
    db.commit()

    def msg(i, de, texto, dia):
        return {"id": f"m{i}", "conversationId": "c", "from": {"emailAddress": {"address": de}},
                "toRecipients": [], "receivedDateTime": f"2026-0{dia}-10T12:00:00Z", "uniqueBody": {"content": texto}}

    class _Graph:
        def __init__(self):
            self.caixas = []

        def mensagens(self, usuario, desde):
            self.caixas.append(usuario)
            return iter([msg(1, "ana@alfa.com.br", "Oi\nAna Martins\nAnalista de RH", 3),
                         msg(2, "bia@alfa.com.br", "Bia Reis\nCEO", 4), msg(3, "zeca@outra.com.br", "Zeca\nDiretor", 5)])

    falso = _ClaudeFalso(json.dumps({"nome": "Ana Martins", "cargo": "Analista de RH", "telefone": ""}))
    leitor = assinatura.LeitorAssinatura(settings, client=falso, modelo="m")
    graph = _Graph()
    res = assinatura.historico(db, settings, leitor, {alfa.id}, client=graph)
    assert res == {"pessoas": 1, "com_emails": 1, "preenchidas": 1} and graph.caixas == ["victor@innoaseguros.com.br"]
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    assert (ana.nome, ana.cargo) == ("Ana Martins", "Analista de RH")
    assert "Zeca" not in falso.chamadas[0]["messages"][0]["content"]
    assert assinatura.historico(db, settings, leitor, {alfa.id}, client=graph)["pessoas"] == 0  # já lida
