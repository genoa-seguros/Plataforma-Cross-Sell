import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from crosssell import tabela
from crosssell.connectors import linkedin as lk
from crosssell.models import Empresa, Noticia, Pessoa
from tests.test_fluxo import carregar


def config(settings):
    return settings.model_copy(update={"n8n_linkedin_webhook_url": "https://n8n.exemplo/webhook/linkedin",
                                       "n8n_token": "segredo-compartilhado", "plataforma_url": "https://plataforma.exemplo",
                                       "linkedin_lote": 3})


class N8nFalso:
    def __init__(self):
        self.recebidos = []

    def client(self):
        def handler(req: httpx.Request):
            self.recebidos.append({"auth": req.headers.get("authorization"), "corpo": json.loads(req.content)})
            return httpx.Response(200, json={"message": "Workflow was started"})
        return httpx.Client(transport=httpx.MockTransport(handler))


def test_disparo_envia_lote_e_nao_reenvia_em_andamento(db, settings):
    s = config(settings)
    carregar(db, s)
    # Sem endereço, todos vão como busca
    assert {a["acao"] for a in lk.alvos(db, s)} == {"buscar"} and len(lk.alvos(db, s)) == 4
    for i, obj in enumerate([*db.scalars(select(Empresa)), *db.scalars(select(Pessoa))]):
        obj.linkedin_url = f"https://linkedin.com/x/{i}"
    db.commit()
    n8n = N8nFalso()
    res = lk.disparar(db, s, n8n.client())
    pedido = n8n.recebidos[0]
    assert pedido["auth"] == "Bearer segredo-compartilhado"
    assert pedido["corpo"]["callback_url"] == "https://plataforma.exemplo/api/integracoes/linkedin/resultados"
    assert res["enviados"] == 3 and len(n8n.recebidos) == 3  # respeita o lote
    assert all(len(p["corpo"]["alvos"]) == 1 for p in n8n.recebidos)  # um alvo por chamada
    enviados = {p["corpo"]["alvos"][0]["id_alvo"] for p in n8n.recebidos}

    # Segundo disparo manda só o que sobrou; os já pedidos ficam aguardando o retorno
    lk.disparar(db, s, n8n.client())
    segundo = {p["corpo"]["alvos"][0]["id_alvo"] for p in n8n.recebidos[3:]}
    assert segundo and not (segundo & enviados)
    assert lk.disparar(db, s, n8n.client()) == {"enviados": 0}

    # Sem retorno depois do prazo, volta para a fila
    depois = datetime.utcnow() + lk.PRAZO_PEDIDO + timedelta(hours=1)
    assert {a["id_alvo"] for a in lk.alvos(db, s, agora=depois)} == enviados | segundo


def test_retorno_atualiza_e_confere_nome(db, settings):
    carregar(db, settings)
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    caio = db.scalar(select(Pessoa).where(Pessoa.email == "caio@beta.com.br"))
    resultados = [
        {"id_alvo": f"P{ana.id}", "tipo": "pessoa", "linkedin_url": "https://linkedin.com/in/ana", "nome": "Ana Souza",
         "headline": "CFO | Finanças industriais", "cargo_atual": "CFO", "empresa_atual": "Grupo Gama",
         "capturado_em": "2026-09-30T10:00:00"},
        {"id_alvo": f"P{caio.id}", "tipo": "pessoa", "linkedin_url": "https://linkedin.com/in/outro", "nome": "Carlos Pereira"},
        {"id_alvo": f"E{alfa.id}", "tipo": "empresa", "linkedin_url": "https://linkedin.com/company/alfa",
         "setor": "Manufatura", "funcionarios": "201-500", "site": "https://alfa.com.br", "sede": "Sorocaba, SP",
         "decisores": [{"nome": "Marta Reis", "headline": "Diretora Jurídica", "linkedin_url": "https://linkedin.com/in/marta"}],
         "posts": [{"texto": "Inauguramos nossa fábrica em Sorocaba!\nObrigado", "data": "2026-09-20",
                    "url": "https://linkedin.com/posts/alfa-1"}],
         "capturado_em": "2026-09-30T10:05:00"},
        {"id_alvo": "E999999", "tipo": "empresa", "erro": "Perfil não encontrado"},
    ]
    res = lk.receber(db, resultados)
    assert res["pessoas"] == 1 and res["empresas"] == 1 and res["nao_confirmado"] == 1
    assert res["decisores_novos"] == 1 and res["erros"] == 1
    assert ana.linkedin_url == "https://linkedin.com/in/ana" and ana.linkedin_headline.startswith("CFO")
    assert caio.linkedin_url is None  # nome não confere: perfil descartado
    assert alfa.funcionarios == 500 and alfa.setor == "Manufatura"
    marta = db.scalar(select(Pessoa).where(Pessoa.nome == "Marta Reis"))
    assert marta.empresa_id == alfa.id and marta.senioridade == "diretor" and marta.fonte == "linkedin"
    assert db.scalar(select(Noticia).where(Noticia.fonte == "Post no LinkedIn")).titulo.startswith("Inauguramos")

    linha = next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "4")
    assert any("hoje está em Grupo Gama" in m["texto"] for m in linha["motivos"])
    assert linha["pessoa"]["linkedin"] == "https://linkedin.com/in/ana"
    assert lk.receber(db, resultados)["ja_lidos"] == 2  # reenvio do n8n não duplica


def test_rota_de_retorno_exige_token(engine, db, settings, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from crosssell.web import app as webapp

    s = config(settings)
    carregar(db, s)
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    Local = sessionmaker(bind=engine, expire_on_commit=False)
    webapp.app.dependency_overrides[webapp.get_db] = lambda: Local()
    monkeypatch.setattr(webapp, "get_settings", lambda: s)
    c = TestClient(webapp.app)
    corpo = {"id_alvo": f"P{ana.id}", "nome": "Ana Souza", "headline": "CFO"}
    url = "/api/integracoes/linkedin/resultados"
    assert c.post(url, json=corpo).status_code == 401
    assert c.post(url, json=corpo, headers={"Authorization": "Bearer errado"}).status_code == 401
    r = c.post(url, json={"resultados": [corpo]}, headers={"Authorization": "Bearer segredo-compartilhado"})
    assert r.status_code == 200 and r.json()["pessoas"] == 1
    webapp.app.dependency_overrides.clear()


def test_formatos_aceitos():
    assert lk._lista("Marta Reis | Diretora | https://x\nJoão Lima | CEO") == [
        {"nome": "Marta Reis", "headline": "Diretora", "linkedin_url": "https://x"},
        {"nome": "João Lima", "headline": "CEO", "linkedin_url": ""}]
    assert lk._lista('[{"nome": "A"}]') == [{"nome": "A"}]
    assert lk._num("1.200") == 1200 and lk._num("51-200") == 200 and lk._num(350) == 350 and lk._num("") is None


def test_busca_escolhe_candidato_certo(db, settings):
    carregar(db, settings)
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))
    beta = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 20))
    ana = db.scalar(select(Pessoa).where(Pessoa.email == "ana@alfa.com.br"))
    caio = db.scalar(select(Pessoa).where(Pessoa.email == "caio@beta.com.br"))
    alvo_ana = next(a for a in lk.alvos(db, settings) if a["id_alvo"] == f"P{ana.id}")
    assert alvo_ana["acao"] == "buscar" and alvo_ana["busca"] == "Ana Souza Metalúrgica Alfa"
    assert alvo_ana["empresa_busca"] == "Metalúrgica Alfa" and alvo_ana["nome"] == "Ana Souza"

    res = lk.receber(db, [
        # Homônima de outra empresa vem primeiro; a certa é a que cita a Metalúrgica Alfa
        {"id_alvo": f"P{ana.id}", "acao": "buscar", "candidatos": [
            {"nome": "Ana Souza", "headline": "Advogada na Souza & Lima", "linkedin_url": "https://linkedin.com/in/ana-adv"},
            {"nome": "Ana Paula Souza", "headline": "CFO | Metalúrgica Alfa", "linkedin_url": "https://linkedin.com/in/ana-cfo"}]},
        # Nenhum candidato confere: vira "não encontrado"
        {"id_alvo": f"P{caio.id}", "acao": "buscar", "candidatos": [
            {"nome": "Caio Lima", "headline": "Engenheiro na Gama", "linkedin_url": "https://linkedin.com/in/caio-gama"}]},
        # Empresa: aceita pelo domínio do site
        {"id_alvo": f"E{alfa.id}", "acao": "buscar", "candidatos": [
            {"nome": "Alfa Metais", "site": "https://alfametais.com", "linkedin_url": "https://linkedin.com/company/x"},
            {"nome": "Metalurgica Alfa", "site": "https://www.alfa.com.br", "linkedin_url": "https://linkedin.com/company/alfa"}]},
        # Empresa: aceita pelo nome sem sufixo societário
        {"id_alvo": f"E{beta.id}", "acao": "buscar", "candidatos": [
            {"nome": "Beta Serviços", "linkedin_url": "https://linkedin.com/company/beta"}]},
    ])
    assert res["encontrados"] == 3 and res["nao_encontrados"] == 1
    assert ana.linkedin_url == "https://linkedin.com/in/ana-cfo"
    assert alfa.linkedin_url == "https://linkedin.com/company/alfa" and beta.linkedin_url == "https://linkedin.com/company/beta"
    assert caio.linkedin_url is None and caio.linkedin_nao_encontrado

    fila = {a["id_alvo"]: a["acao"] for a in lk.alvos(db, settings)}
    # Encontrada pela busca já vale como leitura (título e endereço); não encontrado sai da fila por 30 dias
    assert ana.linkedin_headline == "CFO | Metalúrgica Alfa" and ana.linkedin_em is not None
    assert f"P{ana.id}" not in fila and f"P{caio.id}" not in fila
    sit = lk.situacao(db, settings)
    assert [n["nome"] for n in sit["naoEncontrados"]] == ["Caio Lima"]

    lk.definir_endereco(db, f"P{caio.id}", "https://www.linkedin.com/in/caio-lima")
    assert lk.situacao(db, settings)["naoEncontrados"] == []
    assert {a["id_alvo"]: a["acao"] for a in lk.alvos(db, settings)}[f"P{caio.id}"] == "ler"


def test_link_da_empresa_no_site(db, settings):
    carregar(db, settings)
    alfa = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 10))

    def handler(req):
        if req.url.host == "alfa.com.br":
            return httpx.Response(200, text='<footer><a href="https://br.linkedin.com/company/metalurgica-alfa/">in</a></footer>')
        return httpx.Response(404)
    res = lk.descobrir_por_site(db, settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert res["encontradas"] == 1 and alfa.linkedin_url == "https://br.linkedin.com/company/metalurgica-alfa"
    assert lk.descobrir_por_site(db, settings, httpx.Client(transport=httpx.MockTransport(handler)))["verificadas"] == 0


def test_funcionarios_do_linkedin_entram_no_porte(db, settings):
    carregar(db, settings)
    beta = db.scalar(select(Empresa).where(Empresa.pipedrive_org_id == 20))
    beta.funcionarios, beta.funcionarios_fonte = 8, "pipedrive"
    db.commit()
    saude = lambda: next(x for x in tabela.montar(db, settings) if x["pipedriveId"] == "5")  # noqa: E731
    antes = saude()
    assert any(m["texto"] == "só 8 funcionários" and m["sinal"] == "-" for m in antes["motivos"])
    lk.receber(db, [{"id_alvo": f"E{beta.id}", "tipo": "empresa", "funcionarios": "1.240",
                     "capturado_em": "2026-09-30T10:00:00"}])
    assert beta.funcionarios == 1240 and beta.funcionarios_fonte == "linkedin"  # LinkedIn prevalece
    depois = saude()
    assert depois["empresa"]["funcionarios"] == 1240
    assert any(m["texto"] == "1.240 funcionários: empresa grande" for m in depois["motivos"])
    func = next(c for c in depois["criterios"] if c["nome"] == "funcionarios")
    assert func["valor"] == 1.0 and depois["score"] > antes["score"]


def test_busca_real_mariana_lazaro():
    """Resposta real do Search People (termo "Mariana Lazaro"): só a da SumUp é aceita, e só com a empresa."""
    candidatos = [
        {"nome": "Mariana Lazaro", "headline": "Chief Executive Officer Brazil at SumUp",
         "linkedin_url": "https://www.linkedin.com/in/marianalazaro"},
        {"nome": "Lázaro Mariano", "headline": "Sócio proprietário na davinTI", "linkedin_url": "https://x/1"},
        {"nome": "Mariana Lazero", "headline": "Arquiteta", "linkedin_url": "https://x/2"},
        {"nome": "Mariana Lázaro Tognella", "headline": "Assistente Administrativo", "linkedin_url": "https://x/3"},
        {"nome": "Mariana Lázaro", "headline": "Bacharel em Ciências Biológicas", "linkedin_url": "https://x/4"},
    ]
    sumup = Empresa(razao_social="SumUp Pagamentos Ltda", nome_fantasia="SumUp", nome_normalizado="sumup")
    assert lk._escolher_pessoa(Pessoa(nome="Mariana Lazaro", empresa=sumup), candidatos) == \
        "https://www.linkedin.com/in/marianalazaro"
    assert lk._escolher_pessoa(Pessoa(nome="Mariana Lazaro"), candidatos) is None  # sem empresa: homônimos
    outra = Empresa(razao_social="Gama Ltda", nome_normalizado="gama")
    assert lk._escolher_pessoa(Pessoa(nome="Mariana Lazaro", empresa=outra), candidatos) is None


def test_busca_real_salvy():
    """Resposta real do Search Companies (termo "Salvy"): três homônimas; só uma no Brasil."""
    candidatos = [
        {"nome": "Salvy", "local": "Curitiba, PR", "linkedin_url": "https://www.linkedin.com/company/use-salvy"},
        {"nome": "SALVY", "local": "Caraman, Occitanie", "linkedin_url": "https://www.linkedin.com/company/sarl-salvy-cuisine"},
        {"nome": "Salvy", "local": "London", "linkedin_url": "https://www.linkedin.com/company/salvyreport"},
        {"nome": "Salvy VentureCorp", "local": "", "linkedin_url": "https://www.linkedin.com/company/salvy-venturecorp"},
        {"nome": "SALVY Enterprises, LLC", "local": "", "linkedin_url": "https://www.linkedin.com/company/salvy-enterprises-llc"},
    ]
    salvy = Empresa(razao_social="Salvy Tecnologia Ltda", nome_fantasia="Salvy", nome_normalizado="salvy")
    assert lk._escolher_empresa(salvy, candidatos) == "https://www.linkedin.com/company/use-salvy"
    # Duas no Brasil: desempata pela UF; sem UF, não escolhe
    dupla = candidatos + [{"nome": "Salvy", "local": "São Paulo, SP", "linkedin_url": "https://x/salvy-sp"}]
    assert lk._escolher_empresa(salvy, dupla) is None
    salvy.uf = "PR"
    assert lk._escolher_empresa(salvy, dupla) == "https://www.linkedin.com/company/use-salvy"
