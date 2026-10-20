"""To-dos: mudar a data da atividade (vai para o Pipedrive). Oportunidades: filtro "Ganhos recentes" (quem fechou
negócio no Pipedrive nos últimos 14 dias, mesmo sem card aberto)."""

from datetime import date, timedelta

from sqlalchemy import select

from crosssell.connectors import pipedrive
from crosssell.models import Empresa, Negocio, Usuario
from tests.test_web import H, cenario, entrar  # noqa: F401 (fixture)


def _empresa(db, nome, *negocios) -> Empresa:
    e = Empresa(razao_social=nome, nome_normalizado=nome.lower())
    db.add(e)
    for i, (status, titulo, ganho_dias) in enumerate(negocios):
        n = Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo=f"{nome}{i}", pipeline_id=1,
                    status=status, titulo=titulo, produto=titulo, responsavel_email="victor.boldrini@innoaseguros.com.br")
        if ganho_dias is not None:
            n.ganho_em, n.fim_vigencia = date.today() - timedelta(days=ganho_dias), date.today() + timedelta(days=300)
        db.add(n)
    db.commit()
    return e


def test_ganhos_recentes_mesmo_sem_card_aberto(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    so_ganho = _empresa(db, "Recem Fechada SA", ("ganho", "D&O", 3))
    _empresa(db, "Antiga SA", ("ganho", "D&O", 20))  # ganho há mais de 14 dias: fora
    _empresa(db, "Com Card SA", ("ganho", "Cyber", 1), ("aberto", "E&O", None))

    padrao = c.get("/api/oportunidades").json()
    assert "Recem Fechada SA" not in {o["empresa"]["nome"] for o in padrao["itens"]}  # sem card aberto: só no filtro
    r = c.get("/api/oportunidades?oferta=ganhos").json()
    nomes = [o["empresa"]["nome"] for o in r["itens"]]
    assert set(nomes) == {"Recem Fechada SA", "Com Card SA"} and r["ofertas"]["ganhos"] == len(r["itens"])
    assert nomes.index("Com Card SA") < nomes.index("Recem Fechada SA")  # quem fechou mais recentemente primeiro
    item = next(o for o in r["itens"] if o["empresa"]["id"] == so_ganho.id)
    assert item["ganhos"][0]["produto"] == "D&O" and item["ganhos"][0]["donoNome"].startswith("Victor")
    assert item["vertical"] != "linhas_financeiras" or item["tipo"] == "mais"  # LF recém-ganha não é cross
    assert any(not o["analisada"] for o in r["itens"])  # mostra também as ainda não analisadas, com o que falta
    # A ficha da empresa mostra as oportunidades de quem só tem o ganho recente
    assert c.get(f"/api/oportunidades?empresa={so_ganho.id}").json()["itens"]


def test_mudar_data_da_atividade_vai_para_o_pipedrive(cenario, db):  # noqa: F811
    c, fake = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    victor = db.scalar(select(Usuario).where(Usuario.email.like("victor%")))
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    client = pipedrive.PipedriveClient("x", transport=fake.transport())
    at = pipedrive.criar_atividade(db, client, empresa=alfa, pessoa_id=None, assunto="Ligar", vencimento=date.today(),
                                   responsavel=victor, criada_por=victor)
    nova = date.today() + timedelta(days=3)
    r = c.post(f"/api/atividades/{at.id}/data", headers=H, json={"vencimento": nova.isoformat()})
    assert r.json() == {"ok": True, "vencimento": nova.isoformat()}
    assert fake.atualizadas[-1] == (at.pipedrive_id, {"due_date": nova.isoformat()})
    assert c.post("/api/atividades/9999/data", headers=H, json={"vencimento": nova.isoformat()}).status_code == 404
