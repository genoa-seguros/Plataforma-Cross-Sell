import json

import httpx
from sqlalchemy import select

from crosssell import tabela
from crosssell.connectors import linkedin_planilha as lk
from crosssell.models import Empresa, Noticia, Pessoa
from tests.test_fluxo import carregar


class PlanilhaFalsa:
    def __init__(self, resultados=None):
        self.abas = {"Alvos": [], "Resultados": resultados or []}

    def transport(self):
        def handler(req: httpx.Request):
            aba = req.url.path.split("/values/")[1].split("!")[0]
            if req.method == "GET":
                return httpx.Response(200, json={"values": self.abas[aba]})
            if req.url.path.endswith(":clear"):
                self.abas[aba] = []
                return httpx.Response(200, json={})
            self.abas[aba] = json.loads(req.content)["values"]
            return httpx.Response(200, json={})
        return httpx.MockTransport(handler)


def cliente(planilha):
    return lk.SheetsClient("planilha", token=lambda: "t", transport=planilha.transport())


def test_exporta_alvos_dos_negocios_abertos(db, settings):
    carregar(db, settings)
    planilha = PlanilhaFalsa()
    res = lk.exportar(db, settings, cliente(planilha))
    cab, *linhas = planilha.abas["Alvos"]
    assert cab == lk.ALVOS
    ids = {r[0] for r in linhas}
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    assert f"E{alfa.id}" in ids and f"P{ana.id}" in ids
    assert res["empresas"] == 2 and res["pessoas"] == 2


def test_importa_resultados_e_confere_nome(db, settings):
    carregar(db, settings)
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    caio = db.scalar(select(Pessoa).where(Pessoa.email == "caio@beta.com.br"))
    decisores = json.dumps([{"nome": "Marta Reis", "headline": "Diretora Jurídica", "linkedin_url": "https://linkedin.com/in/marta"}])
    posts = json.dumps([{"texto": "Inauguramos nossa fábrica em Sorocaba!\\nObrigado a todos", "data": "2026-09-20",
                         "url": "https://linkedin.com/posts/alfa-1"}])
    linhas = [lk.RESULTADOS,
              [f"P{ana.id}", "pessoa", "https://linkedin.com/in/ana", "Ana Souza", "CFO | Finanças industriais", "CFO",
               "Grupo Gama", "São Paulo", "", "", "", "", "", "", "2026-09-30 10:00", ""],
              [f"P{caio.id}", "pessoa", "https://linkedin.com/in/outro", "Carlos Pereira", "Vendas", "", "", "", "", "",
               "", "", "", "", "2026-09-30 10:00", ""],
              [f"E{alfa.id}", "empresa", "https://linkedin.com/company/alfa", "Metalúrgica Alfa", "", "", "", "",
               "Manufatura", "201-500", "https://alfa.com.br", "Sorocaba, SP", decisores, posts, "2026-09-30 10:05", ""]]
    res = lk.importar(db, settings, cliente(PlanilhaFalsa(linhas)))
    assert res["pessoas"] == 1 and res["empresas"] == 1 and res["nao_confirmado"] == 1 and res["decisores_novos"] == 1
    assert ana.linkedin_url == "https://linkedin.com/in/ana" and ana.linkedin_headline.startswith("CFO")
    assert caio.linkedin_url is None  # nome não confere: perfil descartado
    assert alfa.funcionarios == 500 and alfa.setor == "Manufatura"
    marta = db.scalar(select(Pessoa).where(Pessoa.nome == "Marta Reis"))
    assert marta.empresa_id == alfa.id and marta.senioridade == "diretor" and marta.fonte == "linkedin"
    assert db.scalar(select(Noticia).where(Noticia.fonte == "Post no LinkedIn")).titulo.startswith("Inauguramos")

    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "4")
    assert any("hoje está em Grupo Gama" in m for m in linha["motivos"])
    assert any("decisor no LinkedIn sem relação ainda: Marta Reis" in m for m in linha["motivos"])
    assert linha["pessoa"]["linkedin"] == "https://linkedin.com/in/ana"

    # Reimportar a mesma planilha não reaplica, e quem já foi lido sai dos alvos
    assert lk.importar(db, settings, cliente(PlanilhaFalsa(linhas)))["ja_lidos"] == 2
    ids = {a["id_alvo"] for a in lk.alvos(db, settings)}
    assert f"P{ana.id}" not in ids and f"E{alfa.id}" not in ids and f"P{caio.id}" in ids


def test_decisores_em_texto_simples():
    assert lk._lista("Marta Reis | Diretora | https://x\nJoão Lima | CEO") == [
        {"nome": "Marta Reis", "headline": "Diretora", "linkedin_url": "https://x"},
        {"nome": "João Lima", "headline": "CEO", "linkedin_url": ""}]
    assert lk._num("1.200") == 1200 and lk._num("51-200") == 200 and lk._num("") is None
