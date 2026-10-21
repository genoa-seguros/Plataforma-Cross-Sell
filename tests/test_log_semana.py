"""To-dos > Feito na semana: linha do tempo do que cada pessoa fez (para a reunião de sexta)."""

from datetime import date, datetime, timedelta

from sqlalchemy import select

from crosssell.connectors import pipedrive
from crosssell.models import Empresa, NotaOportunidade, Pessoa, Usuario
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


def test_log_da_semana_mostra_o_que_a_pessoa_fez_em_ordem(cenario, db):  # noqa: F811
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    rodrigo = db.scalar(select(Usuario).where(Usuario.email.like("rodrigo%")))
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    # Rodrigo cria uma atividade para o Victor (vai pelo Pipedrive) e completa dados à mão
    r = c.post("/api/atividades", headers=H, json={"empresa_id": alfa.id, "assunto": "Apresentar Saúde",
                                                  "vencimento": (date.today() + timedelta(days=3)).isoformat(),
                                                  "responsavel_id": victor.id, "vertical": "saude"})
    assert r.status_code == 200
    at_id = r.json()["id"]
    c.post(f"/api/empresas/{alfa.id}/setor", headers=H, json={"setor": "Metalurgia"})
    c.post(f"/api/pessoas/{ana.id}/ponte", headers=H, json={"ponte": True})
    c.post(f"/api/empresas/{alfa.id}/funcionarios", headers=H, json={"funcionarios": 1200})
    # Uma atualização de outra pessoa não entra no log do Rodrigo
    db.add(NotaOportunidade(empresa_id=alfa.id, vertical="saude", texto="Ligou e pediu proposta", usuario_id=victor.id))
    db.commit()

    log = c.get("/api/todos/log").json()
    assert log["usuario"]["nome"] == rodrigo.nome
    textos = [i["texto"] for i in log["itens"]]
    assert textos == ["criou a atividade «Apresentar Saúde»", "informou o setor: Metalurgia",
                      f"marcou {ana.nome} como ponte", "informou 1.200 funcionários"]
    criou = log["itens"][0]
    assert criou["empresa"] == alfa.razao_social and criou["verticalNome"] == "Saúde"
    assert criou["detalhe"].startswith(f"para {victor.nome} · vence")
    assert log["resumo"]["criadas"] == 1 and log["resumo"]["dados"] == 3 and log["resumo"]["empresas"] == 1

    # O Victor conclui a atividade: aparece no log dele, com a atualização que ele escreveu
    c.post(f"/api/atividades/{at_id}/concluir", headers=H, json={"concluida": True})
    lv = c.get(f"/api/todos/log?usuario={victor.id}").json()
    assert [i["tipo"] for i in lv["itens"]] == ["atualizacao", "concluiu"]
    # Semana passada: vazia
    passada = (date.today() - timedelta(days=7)).isoformat()
    assert c.get(f"/api/todos/log?usuario={victor.id}&ref={passada}").json()["itens"] == []


def test_log_marca_atividade_atrasada(cenario, db):  # noqa: F811
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    rodrigo = db.scalar(select(Usuario).where(Usuario.email.like("rodrigo%")))
    rodrigo.pipedrive_user_id = 3
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    db.commit()
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto="Ligar", responsavel=rodrigo,
                              vencimento=date.today() - timedelta(days=2), criada_por=rodrigo)
    log = c.get("/api/todos/log").json()
    assert [a["assunto"] for a in log["atrasadas"]] == ["Ligar"]
    assert datetime.fromisoformat(log["itens"][0]["em"]).date() <= date.today()
