"""Assistente da plataforma: responde pela documentação e consulta os dados só por ferramentas de leitura."""

import json
from types import SimpleNamespace as NS

from crosssell import assistente
from crosssell.web import app as webapp
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


class _ClaudeRoteiro:
    """Claude falsa que devolve as respostas na ordem e guarda as chamadas."""

    def __init__(self, respostas):
        self.chamadas = []
        roteiro, chamadas = list(respostas), self.chamadas

        class _Msgs:
            def create(self, **kw):
                chamadas.append(json.loads(json.dumps(kw, default=str)))
                return roteiro.pop(0)

        self.beta = NS(messages=_Msgs())


def _usa(nome, entrada, id_="t1"):
    return NS(stop_reason="tool_use", content=[NS(type="text", text="Vou consultar."),
                                               NS(type="tool_use", id=id_, name=nome, input=entrada)])


def _fala(texto):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=texto)])


def test_assistente_consulta_os_dados_e_responde(cenario, settings, monkeypatch):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    falso = _ClaudeRoteiro([_usa("buscar_empresas", {"texto": "Metalúrgica Alfa"}),
                            _usa("oportunidades_da_empresa", {"empresa_id": 0}, "t2"),  # id trocado abaixo
                            _fala("A [Metalúrgica Alfa](#empresa-1) está em **Não atuamos**.")])
    # o segundo passo usa o id que a busca devolveu
    original = webapp._ferramenta

    def ferramenta(db, u, nome, a):
        if nome == "oportunidades_da_empresa":
            a = {"empresa_id": ferramenta.empresa}
        r = original(db, u, nome, a)
        if nome == "buscar_empresas":
            ferramenta.empresa = r[0]["id"]
        return r

    monkeypatch.setattr(webapp, "_ferramenta", ferramenta)
    monkeypatch.setattr(webapp, "assistente_ia", lambda db: assistente.Assistente(settings, client=falso, modelo="m"))

    r = c.post("/api/assistente", headers=H, json={"tela": "#oportunidades", "mensagens": [
        {"papel": "ia", "texto": "Olá! Em que posso ajudar?"},  # a saudação da tela não vai para a IA
        {"papel": "eu", "texto": "Por que a Metalúrgica Alfa não aparece?"}]}).json()
    assert r["texto"] == "A [Metalúrgica Alfa](#empresa-1) está em **Não atuamos**."

    primeira, ultima = falso.chamadas[0], falso.chamadas[-1]
    assert primeira["messages"] == [{"role": "user", "content": "Por que a Metalúrgica Alfa não aparece?"}]
    assert "Score" in primeira["system"][0]["text"] and "cache_control" in primeira["system"][0]  # README no prompt
    assert "Rodrigo" in primeira["system"][1]["text"] and "#oportunidades" in primeira["system"][1]["text"]
    assert {t["name"] for t in primeira["tools"]} >= {"buscar_empresas", "ficha_empresa", "feito_na_semana"}
    busca = json.loads(falso.chamadas[1]["messages"][2]["content"][0]["content"])
    assert busca[0]["nome"] == "Metalúrgica Alfa Ltda"
    ops = json.loads(ultima["messages"][-1]["content"][0]["content"])
    assert ops and {"score", "estagio", "faltando", "motivos"} <= set(ops[0]) and "criterios" not in ops[0]


def test_assistente_sem_pergunta_ou_sem_chave(cenario, monkeypatch):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    assert c.post("/api/assistente", headers=H, json={"mensagens": [{"papel": "ia", "texto": "oi"}]}).status_code == 400
    monkeypatch.setattr(webapp, "get_settings", lambda: NS(anthropic_api_key=""))
    r = c.post("/api/assistente", headers=H, json={"mensagens": [{"papel": "eu", "texto": "oi"}]})
    assert r.status_code == 503 and "chave" in r.json()["detail"]


def test_ferramentas_de_leitura(cenario, db):  # noqa: F811
    c, _ = cenario
    from crosssell.models import Usuario
    u = db.query(Usuario).first()
    assert webapp._ferramenta(db, u, "buscar_empresas", {"texto": "al"}) == {"erro": "Digite ao menos 3 letras do nome."}
    assert "Rodrigo" in {x["nome"] for x in webapp._ferramenta(db, u, "equipe", {})}
    lista = webapp._ferramenta(db, u, "listar_oportunidades", {"vertical": "saude"})
    assert {"total", "fila", "itens"} <= set(lista)
    assert "resumo" in webapp._ferramenta(db, u, "feito_na_semana", {"usuario_id": u.id})
    assert webapp._ferramenta(db, u, "apagar_tudo", {}) == {"erro": "Ferramenta desconhecida: apagar_tudo"}


def test_perguntar_nao_limpa_o_calculo_das_oportunidades(cenario, settings, monkeypatch):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    monkeypatch.setattr(webapp, "assistente_ia",
                        lambda db: assistente.Assistente(settings, client=_ClaudeRoteiro([_fala("Oi!")]), modelo="m"))
    c.get("/api/oportunidades")
    chamadas = []
    monkeypatch.setattr(webapp.OPORTUNIDADES, "limpar", lambda: chamadas.append(1))
    assert c.post("/api/assistente", headers=H, json={"mensagens": [{"papel": "eu", "texto": "oi"}]}).json() == {"texto": "Oi!"}
    assert not chamadas
