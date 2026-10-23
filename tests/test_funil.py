"""Funil de Oportunidades: Não atuamos, Em foco, Atualizado (calculados) e Virou negócio (botão Criar negócio)."""

from datetime import date, datetime, timedelta

from sqlalchemy import select

from crosssell import tabela
from crosssell.connectors import pipedrive
from crosssell.models import Negocio, NotaOportunidade, Usuario
from crosssell.web import app as webapp
from tests.test_estacionar import _lead
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


def _coluna(c, chave, q=""):
    col = next(x for x in c.get(f"/api/oportunidades/funil?vertical=saude{q}").json()["colunas"] if x["chave"] == chave)
    return {x["empresa"]["nome"] for x in col["cards"]}


def test_estagio_por_atividade_e_atualizacao():
    hoje = date(2026, 10, 24)
    nota = NotaOportunidade(texto="x", criada_em=datetime(2026, 10, 1), vale_ate=date(2026, 12, 1))
    assert tabela.estagio(nota, None, hoje)["estagio"] == "atualizado"
    vencida = NotaOportunidade(texto="x", criada_em=datetime(2026, 6, 1), vale_ate=date(2026, 8, 1))
    assert tabela.estagio(vencida, None, hoje)["estagio"] == "nao_atuamos"  # atualização vencida volta para o início
    from crosssell.models import Atividade
    antiga = Atividade(assunto="a", vencimento=hoje, criada_em=datetime(2026, 7, 1))
    nova = Atividade(assunto="b", vencimento=hoje, criada_em=datetime(2026, 9, 1))
    assert tabela.estagio(vencida, antiga, hoje)["estagio"] == "nao_atuamos"  # atividade de antes de vencer não conta
    assert tabela.estagio(vencida, nova, hoje)["estagio"] == "em_foco"
    assert tabela.estagio(None, nova, hoje)["estagio"] == "em_foco"
    assert tabela.estagio(None, None, hoje)["estagio"] == "nao_atuamos"


def test_funil_e_criar_negocio(cenario, db):  # noqa: F811
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    a, b, d = _lead(db, 1), _lead(db, 2), _lead(db, 3)
    a.pipedrive_org_id, b.pipedrive_org_id = 501, 502
    db.commit()
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    pipedrive.criar_atividade(db, client, empresa=b, pessoa_id=None, assunto="Ligar", responsavel=victor,
                              vencimento=date.today(), criada_por=victor, vertical="saude")
    r = c.post(f"/api/empresas/{d.id}/notas", headers=H, json={"vertical": "saude", "texto": "Quer conversar em março", "meses": 6})
    assert r.status_code == 200
    nota = db.scalar(select(NotaOportunidade).where(NotaOportunidade.empresa_id == d.id))
    assert nota.vale_ate == date.today() + timedelta(days=183)
    webapp.OPORTUNIDADES.limpar()
    assert _coluna(c, "nao_atuamos") == {"Lead 1 SA"}
    assert _coluna(c, "em_foco") == {"Lead 2 SA"}
    assert _coluna(c, "atualizado") == {"Lead 3 SA"}

    op = c.get("/api/oportunidades/negocio-opcoes?vertical=saude").json()
    assert [f["nome"] for f in op["funis"]] == ["Saúde", "Pipo Saúde", "Flash Benefícios"]
    assert [p["label"] for p in op["produtos"]] == ["Saúde", "Vida"]
    funil = op["funis"][0]
    r = c.post("/api/oportunidades/negocio", headers=H, json={
        "empresa_id": a.id, "vertical": "saude", "pipeline_id": funil["id"], "stage_id": funil["etapas"][1]["id"],
        "produto_id": 711, "titulo": "Saúde 2026", "responsavel_id": victor.id})
    assert r.json()["ok"], r.text
    criado = fake.negocios_criados[-1]
    assert criado["title"] == "Saúde 2026" and criado["org_id"] == 501 and criado["pipeline_id"] == 23
    assert criado["custom_fields"] == {"74bcdb3b63321d279e6ada0de40e1ca1b36f0171": 711}
    neg = db.scalar(select(Negocio).where(Negocio.id_externo == str(criado["id"])))
    assert neg.status == "aberto" and neg.vertical == "saude" and neg.etapa == "Cotação"
    # A oportunidade sai das colunas (Saúde em negociação) e aparece em "Virou negócio"
    assert "Lead 1 SA" not in _coluna(c, "nao_atuamos") and _coluna(c, "virou") == {"Lead 1 SA"}
    # Sem organização no Pipedrive não dá para criar
    assert c.post("/api/oportunidades/negocio", headers=H, json={
        "empresa_id": d.id, "vertical": "saude", "pipeline_id": funil["id"], "stage_id": funil["etapas"][0]["id"],
        "titulo": "Saúde 2026", "responsavel_id": victor.id}).status_code == 400
