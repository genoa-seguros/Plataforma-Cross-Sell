"""Oportunidades > Estacionar: tira a oportunidade da tela por ora, com o motivo; volta na data de rever ou antes."""

from datetime import date, datetime, timedelta

from sqlalchemy import select

from crosssell.models import Empresa, Negocio, Noticia, OportunidadeEstacionada
from crosssell.web import app as webapp
from tests.test_web import H, cenario, entrar, mapear_rh  # noqa: F401 (fixture)


def _lead(db, i):
    e = Empresa(razao_social=f"Lead {i} SA", nome_normalizado=f"lead {i}", cidade="São Paulo", uf="SP",
                funcionarios=120, setor="Software", funcionarios_fonte="linkedin", setor_fonte="linkedin")
    db.add_all([e, Negocio(empresa=e, vertical="linhas_financeiras", fonte="pipedrive", id_externo=f"9{i}",
                           pipeline_id=1, status="aberto", titulo="D&O")])
    db.flush()
    mapear_rh(db, e, i)
    db.commit()
    return e


def test_estacionar_tira_da_tela_e_trazer_devolve(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    a, b = _lead(db, 1), _lead(db, 2)
    nomes = lambda r: {o["empresa"]["nome"] for o in r["itens"]}  # noqa: E731
    assert nomes(c.get("/api/oportunidades?vertical=saude").json()) == {"Lead 1 SA", "Lead 2 SA"}

    r = c.post("/api/oportunidades/estacionar", headers=H,
               json={"empresa_id": a.id, "vertical": "saude", "motivo": "concorrente", "texto": "corretor da família"})
    assert r.json()["reverEm"] == (date.today() + timedelta(days=183)).isoformat()  # 6 meses por padrão
    padrao = c.get("/api/oportunidades?vertical=saude").json()
    assert nomes(padrao) == {"Lead 2 SA"} and padrao["ofertas"]["estacionadas"] == 1
    paradas = c.get("/api/oportunidades?vertical=saude&oferta=estacionadas").json()
    assert nomes(paradas) == {"Lead 1 SA"}
    est = paradas["itens"][0]["estacionada"]
    assert est["motivoTexto"] == "Corretor ou relação forte com concorrente" and est["texto"] == "corretor da família"
    # Vai para o log da semana e não pede o motivo "outro" sem texto
    assert any(i["tipo"] == "estacionou" for i in c.get("/api/todos/log").json()["itens"])
    assert c.post("/api/oportunidades/estacionar", headers=H,
                  json={"empresa_id": b.id, "vertical": "saude", "motivo": "outro"}).status_code == 400

    assert c.post("/api/oportunidades/trazer", headers=H, json={"empresa_id": a.id, "vertical": "saude"}).json() == {"ok": True}
    assert nomes(c.get("/api/oportunidades?vertical=saude").json()) == {"Lead 1 SA", "Lead 2 SA"}


def test_estacionada_volta_na_data_ou_com_ma_ou_negocio_ganho(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    a = _lead(db, 3)
    c.post("/api/oportunidades/estacionar", headers=H,
           json={"empresa_id": a.id, "vertical": "saude", "motivo": "perfil", "meses": None})
    item = lambda: next(o for o in c.get("/api/oportunidades?vertical=saude").json()["itens"]  # noqa: E731
                        if o["empresa"]["nome"] == "Lead 3 SA") if any(
        o["empresa"]["nome"] == "Lead 3 SA" for o in c.get("/api/oportunidades?vertical=saude").json()["itens"]) else None
    assert item() is None  # sem data: fica estacionada
    x = db.scalar(select(OportunidadeEstacionada).where(OportunidadeEstacionada.empresa_id == a.id))
    # Notícia de M&A depois de estacionar: volta, com o motivo
    db.add(Noticia(empresa_id=a.id, titulo="Lead 3 anuncia aquisição da concorrente", url="http://x/1", fonte="X",
                   publicada_em=datetime.utcnow() + timedelta(minutes=1)))
    db.commit()
    webapp.OPORTUNIDADES.limpar()  # a lista fica guardada até a próxima alteração pela tela
    assert item()["voltou"] == "notícia de M&A ou aporte depois de estacionar"
    # Data de rever que já passou: também volta
    db.query(Noticia).delete()
    x.rever_em = date.today()
    db.commit()
    webapp.OPORTUNIDADES.limpar()
    assert item()["voltou"].startswith("chegou a data de rever")
