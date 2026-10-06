"""Revisão do cadastro: organizações duplicadas e razão social."""

import json

import httpx
from sqlalchemy import event, select

from crosssell import qualidade, tabela
from crosssell.models import Empresa, Negocio
from tests.fakes import DADOS, FakePipedrive
from tests.test_fluxo import carregar


class PipedriveEscrita(FakePipedrive):
    def __init__(self):
        super().__init__()
        self.escritas = []

    def _handler(self, req):
        if req.method in ("PUT", "PATCH") and "/organizations/" in req.url.path:
            self.escritas.append((req.method, req.url.path, json.loads(req.content)))
            return httpx.Response(200, json={"data": {"id": 1}})
        return super()._handler(req)


def test_organizacoes_duplicadas_nao_se_fundem_na_sincronizacao_e_sao_mescladas_com_aprovacao(db, settings, monkeypatch):
    # Duas organizações no Pipedrive com o mesmo CNPJ da Alfa, cada uma com seu negócio
    orgs = DADOS["organizations"] + [{"id": 11, "name": "Metalurgica Alfa", "custom_fields":
                                      {settings.pipedrive_cnpj_field: "14.069.185/0001-03"}}]
    deals = DADOS["deals"] + [{"id": 70, "title": "RC 2026", "pipeline_id": 29, "stage_id": 4, "status": "open",
                               "value": 10, "org_id": 11, "person_id": None, "owner_id": 2, "custom_fields": {}}]
    monkeypatch.setitem(DADOS, "organizations", orgs)
    monkeypatch.setitem(DADOS, "deals", deals)
    fake = PipedriveEscrita()
    client = carregar(db, settings, fake)

    alfas = db.scalars(select(Empresa).where(Empresa.cnpj == "14069185000103")).all()
    assert sorted(e.pipedrive_org_id for e in alfas) == [10, 11]  # cada organização é uma empresa
    neg70 = db.scalar(select(Negocio).where(Negocio.id_externo == "70"))
    assert neg70.empresa.pipedrive_org_id == 11  # o negócio da duplicada não se perde

    grupos = qualidade.duplicadas(db)
    assert len(grupos) == 1 and grupos[0]["certeza"] == "alta" and "mesmo CNPJ" in grupos[0]["motivos"]
    g = grupos[0]
    assert g["manter"]["orgId"] == 10 and [m["orgId"] for m in g["mesclar"]] == [11]  # fica a que tem ganhos

    res = qualidade.mesclar(db, client, g["manter"]["id"], [m["id"] for m in g["mesclar"]])
    assert res == {"mescladas": 1, "manter": 10}
    assert fake.escritas == [("PUT", "/api/v1/organizations/11/merge", {"merge_with_id": 10})]
    db.expire_all()
    assert db.scalar(select(Negocio).where(Negocio.id_externo == "70")).empresa.pipedrive_org_id == 10
    assert qualidade.duplicadas(db) == []


def test_ignorar_duplicada_e_razao_social(db, settings):
    fake = PipedriveEscrita()
    client = carregar(db, settings, fake)
    beta = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 20))
    db.add(Empresa(razao_social="Beta Serviços S.A.", nome_normalizado=beta.nome_normalizado, pipedrive_org_id=21))
    beta.razao_receita = "BETA SERVICOS DE LIMPEZA E CONSERVACAO LTDA"
    db.commit()
    g = qualidade.duplicadas(db)
    assert len(g) == 1 and g[0]["motivos"] == ["mesmo nome"] and g[0]["certeza"] == "média"
    qualidade.ignorar(db, g[0]["chave"])
    assert qualidade.duplicadas(db) == []  # "não é duplicada" não volta

    r = qualidade.razao_social(db)
    sug = next(s for s in r["sugestoes"] if s["id"] == beta.id)
    assert sug["atual"] == "Beta Serviços SA" and sug["sugerida"] == "Beta Serviços de Limpeza e Conservacao Ltda"
    qualidade.aplicar_razao(db, client, beta.id, sug["sugerida"])
    assert fake.escritas[-1] == ("PATCH", "/api/v2/organizations/20", {"name": "Beta Serviços de Limpeza e Conservacao Ltda"})
    assert not any(s["id"] == beta.id for s in qualidade.razao_social(db)["sugestoes"])
    assert any(x["empresa"] and x["empresa"]["nome"].startswith("Beta Serviços de") for x in tabela.montar(db, settings))


def test_paginas_de_20_e_consultas_fixas(db, settings, engine):
    carregar(db, settings, PipedriveEscrita())
    for i in range(45):
        e = Empresa(razao_social=f"Loja {i}", nome_normalizado=f"loja {i}", pipedrive_org_id=1000 + i,
                    cnpj=f"{i:014d}", razao_receita=f"LOJA {i} COMERCIO DE ROUPAS LTDA")
        db.add(e)
        db.add(Negocio(empresa=e, fonte="pipedrive", id_externo=f"x{i}", status="ganho", vertical="saude", titulo="Saúde"))
    db.commit()
    db.expire_all()

    consultas = []

    def contar(*_):
        consultas.append(1)

    event.listen(engine, "before_cursor_execute", contar)
    try:
        p3 = qualidade.razao_social(db, 3)
        qualidade.duplicadas(db)
    finally:
        event.remove(engine, "before_cursor_execute", contar)
    assert len(consultas) <= 10  # antes: uma consulta por organização
    total = p3["sugestoesPagina"]["total"]
    assert total >= 45 and p3["sugestoesPagina"]["paginas"] == -(-total // 20) and p3["sugestoesPagina"]["pagina"] == 3
    assert len(p3["sugestoes"]) == total - 40
    todas = qualidade.razao_social(db)["sugestoes"]
    assert [s["id"] for s in p3["sugestoes"]] == [s["id"] for s in todas[40:60]]
    assert qualidade.razao_social(db, 99)["sugestoesPagina"]["pagina"] == 3  # além da última vai para a última
    assert qualidade.pagina([], 5) == {"itens": [], "total": 0, "pagina": 1, "paginas": 1}
