"""Fase 3 de Linhas Financeiras: portais de notícias, site lido pela IA, oportunidades por produto e o fluxo do
LinkedIn (cargos-chave, cota Pipo/outros, M&A antecipa a releitura)."""

import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from crosssell import site_ia
from crosssell.connectors import linkedin as lk
from crosssell.connectors import noticias
from crosssell.models import Empresa, Interacao, LinkedinPedido, Negocio, Noticia, Pessoa
from tests.test_fluxo import _ClaudeFalso, carregar
from tests.test_linkedapi import LinkedApiFalsa, config
from tests.test_web import cenario, entrar  # noqa: F401 (fixture)


def _rss(*itens):
    corpo = "".join(f"<item><title>{t} - {f}</title><source url=\"{u}\">{f}</source><link>{link}</link>"
                    f"<pubDate>Mon, 28 Sep 2026 10:00:00 GMT</pubDate></item>" for t, f, u, link in itens)
    return f"<rss><channel>{corpo}</channel></rss>"


def test_noticias_buscam_tambem_nos_portais(db):
    e = Empresa(razao_social="Dados Seguros SA", nome_fantasia="Dados Seguros", nome_normalizado="dados seguros")
    db.add(e)
    db.commit()
    consultas = []

    def handler(req: httpx.Request):
        q = parse_qs(urlparse(str(req.url)).query)["q"][0]
        consultas.append(q)
        if "site:neofeed.com.br" in q:
            return httpx.Response(200, text=_rss(("Dados Seguros é comprada pela Big Tech", "NeoFeed",
                                                  "https://neofeed.com.br", "https://news.google.com/p1")))
        return httpx.Response(200, text=_rss(("Dados Seguros abre vagas", "G1", "https://g1.globo.com",
                                              "https://news.google.com/g1"),
                                             ("Dados Seguros é comprada pela Big Tech", "NeoFeed",
                                              "https://neofeed.com.br", "https://news.google.com/p1")))

    res = noticias.atualizar(db, [e.id], http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert res == {"empresas": 1, "noticias": 2}  # a do portal veio nas duas buscas: grava uma vez
    assert consultas[0] == '"Dados Seguros"'
    assert all(f"site:{d}" in consultas[1] for d in ("neofeed.com.br", "braziljournal.com", "valor.globo.com", "exame.com"))
    sites = {n.titulo: n.site for n in db.scalars(select(Noticia))}
    assert noticias.portal(sites["Dados Seguros é comprada pela Big Tech"]) == "NeoFeed"
    assert noticias.portal(sites["Dados Seguros abre vagas"]) is None
    assert noticias.portal("https://www.exame.com/negocios") == "Exame"


SITE = """<html><head><title>Arquitetura Viva</title><meta name="description" content="Escritório de arquitetura">
<script>var x = "não entra";</script></head><body><h1>Projetos corporativos</h1>
<p>Há 20 anos criamos sedes para grandes empresas. Nossos projetos atendem clientes em todo o Brasil, com equipe de
arquitetos, engenheiros e designers dedicados a cada etapa.</p>
<a href="/clientes">Clientes</a> <a href="https://outro.com/sobre">fora</a> <a href="/blog/post">blog</a></body></html>"""
CLIENTES = "<html><body><h2>Clientes</h2><p>Natura, Itaú, Ambev e outras marcas confiam em nós.</p></body></html>"
RESPOSTA = {"fundo_gestora": False, "fundos_investidores": False, "investidores": [], "grandes_clientes": True,
            "clientes": ["Natura", "Itaú", "Ambev"], "servico_intelectual": True, "site_profissional": True,
            "servico": "projetos de arquitetura corporativa", "resumo": "Escritório de arquitetura com grandes clientes."}


def test_site_lido_pela_ia(db, settings):
    viva = Empresa(razao_social="Arquitetura Viva Ltda", nome_normalizado="arquitetura viva", dominio="viva.com.br")
    fora = Empresa(razao_social="Fora do Ar Ltda", nome_normalizado="fora do ar", dominio="foradoar.com.br")
    sem = Empresa(razao_social="Sem Site Ltda", nome_normalizado="sem site")
    db.add_all([viva, fora, sem])
    db.commit()
    pedidas = []

    def handler(req: httpx.Request):
        pedidas.append(str(req.url))
        if req.url.host == "foradoar.com.br":
            raise httpx.ConnectError("sem resposta")
        if req.url.path == "/clientes":
            return httpx.Response(200, text=CLIENTES, headers={"content-type": "text/html"})
        return httpx.Response(200, text=SITE, headers={"content-type": "text/html; charset=utf-8"})

    falso = _ClaudeFalso(json.dumps(RESPOSTA))
    leitor = site_ia.LeitorSite(settings, client=falso, modelo="claude-sonnet-5-5")
    res = site_ia.atualizar(db, [viva.id, fora.id, sem.id], leitor, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert res == {"lidos": 1, "sem_site": 1, "falhas_ia": 0}  # sem site: nem tenta
    # Página inicial e a interna de clientes (do mesmo site); o link de outro site e o blog ficam de fora
    assert pedidas == ["https://viva.com.br", "https://viva.com.br/clientes", "https://foradoar.com.br"]
    texto = falso.chamadas[0]["messages"][0]["content"]
    assert "Natura, Itaú" in texto and "não entra" not in texto and "Escritório de arquitetura" in texto
    assert falso.chamadas[0]["output_config"]["effort"] == "low"
    assert viva.site_ia["grandes_clientes"] and viva.site_ia["paginas"] == ["https://viva.com.br", "https://viva.com.br/clientes"]
    assert "Projetos corporativos" not in json.dumps(viva.site_ia, ensure_ascii=False)  # o texto do site não fica
    assert fora.site_ia == {"erro": "site fora do ar", "site": "https://foradoar.com.br"} and fora.site_ia_em
    assert sem.site_ia_em is None
    # Lido: só volta depois da validade
    assert site_ia.atualizar(db, [viva.id, fora.id], leitor, http=httpx.Client(transport=httpx.MockTransport(handler))) == \
        {"lidos": 0, "sem_site": 0, "falhas_ia": 0}


def _lf_analisada(db, nome: str, i: int, **kw) -> Empresa:
    """Lead negociando Saúde, com tudo de LF lido: Receita, notícias, site (IA), cargos-chave, CFO com ponte."""
    agora = datetime.utcnow()
    e = Empresa(razao_social=nome, nome_normalizado=nome.lower(), enriquecido_em=agora, noticias_em=agora,
                dominio=f"lf{i}.com.br", site_ia_em=agora, site_ia={**RESPOSTA, "site": f"https://lf{i}.com.br"},
                linkedin_areas={lk.PESSOAS_LF: agora.isoformat()}, **kw)
    cfo = Pessoa(nome=f"Clara CFO {i}", nome_normalizado=f"clara cfo {i}", empresa=e, cargo="CFO",
                 email=f"clara@lf{i}.com.br")
    db.add_all([e, cfo, Negocio(empresa=e, vertical="saude", fonte="pipedrive", id_externo=f"lf{i}", pipeline_id=23,
                                status="aberto", titulo="Saúde")])
    db.flush()
    for d in ("recebido", "enviado"):
        db.add(Interacao(message_id=f"lf{i}{d}", data=agora, usuario_email="victor.boldrini@innoaseguros.com.br",
                         email_externo=cfo.email, direcao=d, pessoa_id=cfo.id, empresa_id=e.id))
    return e


def test_lf_analisada_entra_em_oportunidades_por_produto(cenario, db):  # noqa: F811
    c, _ = cenario
    entrar(c, "rodrigo.pedroni@innoaseguros.com.br", "senha-do-master-123")
    viva = _lf_analisada(db, "Arquitetura Viva Ltda", 1)
    # Negociando D&O e já ofertado Cyber (perdido): "mais produtos em LF"
    mais = _lf_analisada(db, "Consultoria Mais SA", 2, natureza_juridica="205-4 - Sociedade Anônima Fechada")
    db.add_all([Negocio(empresa=mais, vertical="linhas_financeiras", fonte="pipedrive", id_externo="m1", pipeline_id=1,
                        status="aberto", titulo="D&O 2026", produto="D&O"),
                Negocio(empresa=mais, vertical="linhas_financeiras", fonte="pipedrive", id_externo="m2", pipeline_id=1,
                        status="perdido", titulo="Cyber", produto="Cyber", perdido_em=datetime(2026, 3, 5).date())])
    mei = _lf_analisada(db, "Joana Silva MEI", 3, porte="MEI")
    db.commit()

    r = c.get("/api/oportunidades").json()
    lf = [o for o in r["itens"] if o["vertical"] == "linhas_financeiras"]
    assert [o["empresa"]["nome"] for o in lf] == ["Arquitetura Viva Ltda"]  # padrão: cross sell
    assert lf[0]["tipo"] == "cross" and lf[0]["analisada"] and lf[0]["etiquetas"] == []
    assert [x["produto"] for x in lf[0]["produtos"]][0] == "E&O"
    assert lf[0]["quemDecide"]["pessoas"][0]["nome"] == "Clara CFO 1"  # Financeiro decide LF
    assert r["ofertas"] == {"cross": 1, "mais_lf": 1}

    mais_lf = c.get("/api/oportunidades?oferta=mais_lf").json()["itens"]
    assert [o["empresa"]["nome"] for o in mais_lf] == ["Consultoria Mais SA"]
    assert mais_lf[0]["etiquetas"] == ["negociando D&O agora", "Cyber ofertado em 03/2026, perdido"]
    assert "D&O" not in [x["produto"] for x in mais_lf[0]["produtos"]]

    # MEI: fora de LF para sempre, nem na fila
    ficha = next(o for o in c.get(f"/api/oportunidades?empresa={mei.id}").json()["itens"]
                 if o["vertical"] == "linhas_financeiras")
    assert ficha["faltando"] == ["MEI"] and r["fila"]["linhas_financeiras"] == 2  # Alfa e Beta do Pipedrive falso

    # Sem a lista de cargos-chave ainda não é analisada
    viva.linkedin_areas = {}
    db.commit()
    c.post("/api/melhorias", headers={"X-Cross-Sell": "1"}, json={"titulo": "limpa a lista guardada"})
    ficha = next(o for o in c.get(f"/api/oportunidades?empresa={viva.id}").json()["itens"]
                 if o["vertical"] == "linhas_financeiras")
    assert ficha["faltando"] == ["cargos-chave"]

    # Sem CNPJ e já procurado no site, a Receita não trava; nem a empresa que não está no LinkedIn
    viva.enriquecido_em, viva.cnpj, viva.site_cnpj_em = None, None, datetime.utcnow()
    viva.linkedin_nao_encontrado = True
    db.commit()
    c.post("/api/melhorias", headers={"X-Cross-Sell": "1"}, json={"titulo": "limpa a lista guardada"})
    ficha = next(o for o in c.get(f"/api/oportunidades?empresa={viva.id}").json()["itens"]
                 if o["vertical"] == "linhas_financeiras")
    assert ficha["analisada"]


def test_linkedin_lf_cota_pipo_e_ma_antecipa_releitura(db, settings):
    s = config(settings, linkedin_sales_navigator=True)
    assert lk.cotas(s)[("linhas_financeiras", "pipo")] == 5 and lk.cotas(s)[("linhas_financeiras", "outros")] == 5
    carregar(db, s)
    # Lead que só negocia Saúde (fora do Pipo): LF é cross sell, cota "outros"
    e = Empresa(razao_social="Fintech Nova SA", nome_normalizado="fintech nova", setor="Fintech", setor_fonte="linkedin",
                funcionarios=300, funcionarios_fonte="linkedin", linkedin_url="https://www.linkedin.com/company/fintech-nova",
                linkedin_em=datetime.utcnow(), investida=True, linkedin_areas={"urn": "urn:li:fsd_company:777"},
                cidade="São Paulo", uf="SP", cidade_fonte="manual")
    db.add_all([e, Negocio(empresa=e, vertical="saude", fonte="pipedrive", id_externo="fn1", pipeline_id=23,
                           status="aberto", titulo="Saúde")])
    db.commit()
    lf = lambda agora=None: [a for a in lk.alvos(db, s, agora) if a["id_alvo"] == f"E{e.id}"  # noqa: E731
                             and a["vertical"] == "linhas_financeiras"]
    # Página já lida com VC, setor e funcionários: só a lista de cargos-chave
    assert [(a["acao"], a["grupo"], a["area"]) for a in lf()] == [("pessoas", "outros", "linhas_financeiras")]
    assert "ciso" in lf()[0]["cargos"]

    api = LinkedApiFalsa()
    for _ in range(2):
        lk.executar(db, s, api.client())
    db.expire_all()
    assert lk.PESSOAS_LF in e.linkedin_areas  # data própria (a lista de RH do negócio de Saúde tem a dela)
    ped = db.scalar(select(LinkedinPedido).where(LinkedinPedido.id_alvo == f"E{e.id}", LinkedinPedido.acao == "pessoas",
                                                 LinkedinPedido.area == "linhas_financeiras"))
    assert (ped.vertical, ped.grupo) == ("linhas_financeiras", "outros")
    assert not lf()  # lida: nada até a validade...
    # ...a não ser que saia notícia de M&A depois da última leitura
    e.noticias.append(Noticia(titulo="Fintech Nova é adquirida pelo Banco X", url="https://n/1",
                              publicada_em=datetime.utcnow() + timedelta(minutes=5)))
    db.commit()
    assert [(a["acao"], a["motivo"]) for a in lf()] == [("pessoas", "Linhas Financeiras: notícia de M&A/aporte")]

    # Com card no funil Pipo Saúde, a cota é a do Pipo
    db.add(Negocio(empresa=e, vertical="saude", fonte="pipedrive", id_externo="fn2", pipeline_id=34, status="aberto",
                   titulo="Pipo"))
    db.commit()
    assert lk.grupo_lf(e, s) == "pipo"
