"""Tabela de trabalho: negócios ABERTOS dos funis marcados com `tabela: true`, e a aba
Oportunidades (verticais que a empresa ainda não tem nem está negociando).

Cada linha tem dois números (ver crosssell/potencial.py):
  Potencial   critérios de um bom negócio na vertical (ordena a tabela)
  Influência  quem é o contato: relacionamento por e-mail + hierarquia do cargo

"Por quê" traz só o que não aparece em outra coluna: critérios a favor e contra,
reconquista e contato que mudou de empresa.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from crosssell.config import AREAS_VERTICAL, VERTICAIS, VERTICAL_LABEL, Settings
from crosssell.connectors.linkedin import FUNCIONARIOS, PESSOAS, PESSOAS_LF, PESSOAS_RE, mudou_de_empresa
from crosssell.connectors.noticias import portal
from crosssell.site_ia import endereco
from crosssell.models import (AlteracaoDado, AnaliseDia, Atividade, Empresa, Interacao, LinkedinPedido, Negocio, NotaOportunidade,
                               OportunidadeEstacionada, OportunidadeManual, Pessoa, Registro, SyncLog, Usuario)
from crosssell.normalize import AREA_LABEL, classificar_area, sem_acento
from crosssell.potencial import (FITS, FITS_RE, areas_re, faltando_saude, funcionarios_validos, influencia, mei, noticias_ma,
                                 melhor_contato, motivos as motivos_potencial, potencial, praca, produto_lf, produto_re,
                                 produtos_lf, produtos_re, setor_valido)

# Verticais que já têm fluxo de análise definido e aparecem em Oportunidades
VERTICAIS_OPORTUNIDADES = ("saude", "linhas_financeiras", "ramos_elementares")
# Nunca entram em Oportunidades, nem na fila de análise
FINAIS = ("fora da praça", "MEI", "sem produto de RE")
# Filtro "Ganhos recentes" em Oportunidades: quem fechou negócio no Pipedrive nestes dias (abordar com a Innoa fresca)
GANHOS_DIAS = 14
# "Estacionar": tira a oportunidade da tela por ora, com o motivo (vira a lista apartada "Estacionadas")
MOTIVOS_ESTACIONAR = {"sem_perspectiva": "Já trabalhamos, sem perspectiva agora",
                      "concorrente": "Corretor ou relação forte com concorrente",
                      "perfil": "Score/perfil não compensa", "outro": "Outro"}
# Filtro "Score alto, influência baixa": boas oportunidades onde falta relação com quem decide
SCORE_ALTO, INFLUENCIA_BAIXA = 70, 40


def faltando_lf(e: Empresa | None) -> list[str]:
    """Linhas Financeiras: Receita lida, notícias buscadas, site lido pela IA (quando há site) e a lista de
    cargos-chave do LinkedIn lida. MEI nunca entra."""
    if mei(e):
        return ["MEI"]
    if e is None:
        return ["Receita"]
    falta = []
    # Sem CNPJ, depois de procurado no site sem sucesso, a Receita não tem como ser lida: não trava a análise
    if e.enriquecido_em is None and (e.cnpj or (e.dominio and e.site_cnpj_em is None)):
        falta.append("Receita")
    if e.noticias_em is None:
        falta.append("notícias")
    if e.site_ia_em is None and endereco(e):
        falta.append("site (IA)")
    # Empresa não encontrada no LinkedIn também não trava (quem decide pode vir dos e-mails)
    lida = (e.linkedin_areas or {}).get(PESSOAS_LF) or (e.linkedin_areas or {}).get(FUNCIONARIOS)
    if not lida and not (e.linkedin_nao_encontrado and not e.linkedin_url):
        falta.append("cargos-chave")
    return falta


def faltando_re(e: Empresa | None) -> list[str]:
    """RE: setor e funcionários (LinkedIn ou à mão): dizem quais produtos fazem sentido (Fiança: escritório com
    50+ funcionários; Patrimonial: grandes ativos; Transportes: cargas e frota)."""
    falta = []
    if not setor_valido(e):
        falta.append("setor")
    if not funcionarios_validos(e):
        falta.append("funcionários")
    return falta


def faltando(vertical: str, e: Empresa | None, decide: dict | None = None, tem_ponte: bool = False,
             sem_produto: bool = False) -> list[str]:
    """O que falta para a empresa entrar em Oportunidades na vertical (vazio = analisada).
    Saúde: praça alvo, funcionários e setor (LinkedIn ou à mão). LF: Receita, notícias, site e cargos-chave.
    RE: setor e funcionários. Em todas: quem decide mapeado (a área da vertical ou do produto, ou um executivo) e
    ponte por e-mail (alguém da empresa que
    troca e-mails com a equipe). Sem isso a equipe teria de buscar na mão."""
    if vertical == "saude":
        falta = faltando_saude(e)
    elif vertical == "linhas_financeiras":
        falta = faltando_lf(e)
    elif vertical == "ramos_elementares":
        falta = faltando_re(e)
        # Setor e funcionários conhecidos e nenhum produto faz sentido (ex.: escritório pequeno): RE não entra
        if not falta and sem_produto:
            return ["sem produto de RE"]
    else:
        return ["fluxo da vertical"]
    if any(f in FINAIS for f in falta):
        return [f for f in falta if f in FINAIS]
    # Alguém da área (RH em Saúde; financeiro, jurídico ou riscos em LF) ou, sem ela, um executivo (founder, CEO,
    # CFO). Sócio que só veio da Receita não conta: numa empresa maior, o sócio-administrador registrado raramente
    # é quem decide
    mapeados = [p for p in (decide or {}).get("pessoas", []) if (decide or {}).get("daArea") or p.get("fonte") != "receita"]
    if not mapeados:
        falta.append("quem decide")
    if not tem_ponte:
        falta.append("ponte por e-mail")
    return falta


PONTE_DIAS = 365  # a ponte lembra de 12 meses de e-mails (o Score de Influência continua valorizando o recente)


def com_relacao(db: Session, empresas) -> set[int]:
    """Pessoas dessas empresas que trocaram e-mails de verdade com a equipe nos últimos 12 meses: escreveram para
    alguém da equipe e receberam e-mail da equipe."""
    desde = datetime.utcnow() - timedelta(days=PONTE_DIAS)
    lados: dict[int, set[str]] = defaultdict(set)
    q = (select(Interacao.pessoa_id, Interacao.direcao).join(Pessoa, Pessoa.id == Interacao.pessoa_id)
         .where(Pessoa.empresa_id.in_(empresas), Interacao.data >= desde).distinct())
    for pessoa_id, direcao in db.execute(q):
        lados[pessoa_id].add(direcao)
    return {pid for pid, d in lados.items() if {"enviado", "recebido"} <= d}


def tem_relacao(p: Pessoa, relacao: set[int] | None) -> bool:
    """Ponte: troca e-mails com a equipe ou foi marcada à mão (conhecido pessoal, WhatsApp)."""
    return p.ponte_manual or (p.score_relacionamento or 0) >= 20 or (relacao is not None and p.id in relacao)


FONTE_FUNC = {"linkedin": "no LinkedIn", "pipedrive": "no Pipedrive", "planilha": "na planilha", "manual": "informado à mão"}
DECISORES = {"socio", "c_level", "diretor"}
NIVEL = {"socio": 0, "c_level": 0, "diretor": 1, "gerente": 2}


def area_pessoa(p: Pessoa) -> str | None:
    return classificar_area(p.cargo) or classificar_area(p.linkedin_headline)


def _pessoa_json(db: Session, p: Pessoa, nomes: dict[str, str], pontes: dict[int, str] | None = None) -> dict:
    proximo = pontes.get(p.id) if pontes is not None else _ponte(db, p.id, None)
    area = area_pessoa(p)
    return {"id": p.id, "nome": p.nome, "cargo": p.cargo or p.linkedin_headline, "area": area,
            "areaNome": AREA_LABEL.get(area or ""), "linkedin": p.linkedin_url, "fonte": p.fonte,
            "noPipedrive": bool(p.pipedrive_person_id), "relacao": round(p.score_relacionamento or 0),
            "temperatura": p.temperatura, "quemFala": nomes.get(proximo, proximo) if proximo else None,
            "influencia": influencia(p)["score"], "ponteManual": p.ponte_manual}


def quem_decide(db: Session, e: Empresa | None, vertical: str | None, nomes: dict[str, str],
                pontes: dict[int, str] | None = None, relacao: set[int] | None = None,
                areas: tuple[str, ...] | None = None) -> dict | None:
    """Pessoas da área que costuma decidir a vertical e, se ninguém dela tem relação, a ponte:
    o contato da empresa com relação mais forte com alguém da equipe."""
    if e is None or vertical not in AREAS_VERTICAL:
        return None
    areas = areas or AREAS_VERTICAL[vertical]
    chave = lambda p: (-(p.score_relacionamento or 0), NIVEL.get(p.senioridade or "", 3), p.nome)  # noqa: E731
    da_area = sorted((p for p in e.pessoas if area_pessoa(p) in areas), key=chave)
    # Executivo do LinkedIn ou dos e-mails antes do sócio que só veio da Receita
    executivos = sorted((p for p in e.pessoas if area_pessoa(p) == "executivo"), key=lambda p: (p.fonte == "receita", *chave(p)))
    pessoas = da_area[:3] or executivos[:1]
    ponte = None
    if not any(tem_relacao(p, relacao) for p in pessoas):
        rel = sorted((p for p in e.pessoas if tem_relacao(p, relacao) and p not in pessoas), key=chave)
        ponte = _pessoa_json(db, rel[0], nomes, pontes) if rel else None
    # Sem ponte por e-mail: mostra quem temos lá dentro pelo Pipedrive (o contato do negócio aberto mais recente,
    # senão o contato da organização), para a equipe saber com quem falar
    contato = None
    if ponte is None:
        negocios = sorted((n for n in e.negocios if n.status == "aberto" and n.fonte == "pipedrive" and n.pessoa),
                          key=lambda n: n.id, reverse=True)
        candidatos = [(n.pessoa, n.produto or n.titulo) for n in negocios]
        candidatos += [(p, None) for p in sorted(e.pessoas, key=chave) if p.pipedrive_person_id]
        escolhido = next(((p, prod) for p, prod in candidatos if p not in pessoas), None)
        if escolhido:
            contato = {**_pessoa_json(db, escolhido[0], nomes, pontes), "negocio": escolhido[1]}
    return {"areas": [AREA_LABEL[a] for a in areas], "pessoas": [_pessoa_json(db, p, nomes, pontes) for p in pessoas],
            "daArea": bool(da_area), "ponte": ponte, "contato": contato}


def seguros_vigentes(e: Empresa | None) -> list[Negocio]:
    """Seguros vigentes. A marcação manual de Saúde não se repete quando há Saúde ganho no Pipedrive."""
    if e is None:
        return []
    vig = [n for n in e.negocios if n.vigente and n.vertical]
    if any(n.saude_vitalicio for n in vig):
        vig = [n for n in vig if not (n.fonte == "manual" and n.vertical == "saude")]
    return sorted(vig, key=lambda n: (n.vertical, n.produto or n.titulo or ""))


def estado_saude(e: Empresa | None, vigentes: list[Negocio]) -> dict:
    """Situação de Saúde para a caixa da tabela.
    pipedrive: data do último Saúde ganho (contrato sem fim: vale até alguém desmarcar);
    confirmado/desmarcado: o que a equipe informou na plataforma."""
    negs = e.negocios if e else []
    manual = next((x for x in negs if x.fonte == "manual" and x.vertical == "saude"), None)
    ganhos = [x for x in negs if x.saude_vitalicio]
    ultimo = max((x.ganho_em for x in ganhos if x.ganho_em), default=None)
    return {"zeca": any(v.vertical == "saude" and v.fonte == "zeca" for v in vigentes),
            "manual": bool(manual and manual.status == "ativo"),
            "pipedrive": (ultimo.isoformat() if ultimo else "sem data") if ganhos else None,
            "desmarcado": bool(manual and manual.status == "cancelado")}


def _vig_json(v: Negocio) -> dict:
    return {"vertical": v.vertical, "produto": v.produto or v.titulo,
            "fim": v.fim_vigencia.isoformat() if v.fim_vigencia else None, "fonte": v.fonte,
            "ganhoEm": v.ganho_em.isoformat() if v.ganho_em else None, "vitalicio": v.saude_vitalicio}


def _ponte(db: Session, pessoa_id: int | None, empresa_id: int | None) -> str | None:
    q = select(Interacao.usuario_email)
    if pessoa_id:
        q = q.where(Interacao.pessoa_id == pessoa_id)
    elif empresa_id:
        q = q.where(Interacao.empresa_id == empresa_id)
    else:
        return None
    cont = Counter(db.scalars(q))
    return cont.most_common(1)[0][0] if cont else None


def _pontes(db: Session, empresas) -> dict[int, str]:
    """_ponte de todas as pessoas dessas empresas numa consulta só."""
    cont: dict[int, Counter] = defaultdict(Counter)
    q = (select(Interacao.pessoa_id, Interacao.usuario_email).join(Pessoa, Pessoa.id == Interacao.pessoa_id)
         .where(Pessoa.empresa_id.in_(empresas)).order_by(Interacao.id))
    for pessoa_id, email in db.execute(q):
        cont[pessoa_id][email] += 1
    return {pid: c.most_common(1)[0][0] for pid, c in cont.items()}


def _proxima_atividade(db: Session, negocio_id: int) -> Atividade | None:
    return db.scalar(select(Atividade).where(Atividade.negocio_id == negocio_id, Atividade.concluida.is_(False))
                     .order_by(Atividade.vencimento))


def local(e: Empresa | None) -> str | None:
    """"Campinas/SP": cidade e UF da Receita, do Pipedrive ou do LinkedIn ("Joinville, SC")."""
    if e is None or not (e.cidade or e.uf):
        return None
    partes = [x.strip() for x in (e.cidade or "").split(",")]
    cidade = partes[0].title() if partes[0].isupper() else partes[0]
    uf = (e.uf or (partes[1] if len(partes) > 1 and len(partes[1]) == 2 else "")).upper()
    return "/".join(x for x in (cidade, uf) if x) or None


def _funcionarios(n: Negocio | None, e: Empresa | None) -> tuple[int | None, str]:
    if n is not None and n.vertical == "saude" and n.vidas:
        return n.vidas, "vidas no negócio"
    if funcionarios_validos(e):
        return e.funcionarios, f"funcionários {FONTE_FUNC.get(e.funcionarios_fonte or '', '')}".strip()
    return None, ""


def _extras(e: Empresa | None, p: Pessoa | None, vertical: str | None, vigentes: list[Negocio]) -> list[dict]:
    """Motivos que não saem dos critérios: reconquista e contato que mudou de empresa."""
    saida = []
    tem = {v.vertical for v in vigentes}
    ex = [x for x in (e.negocios if e else []) if x.vertical == vertical and x.ex_cliente
          and (x.fim_vigencia or x.saude_vitalicio)]
    if ex and vertical not in tem:
        ultimo = max(ex, key=lambda x: x.fim_vigencia or date.max)
        if ultimo.saude_vitalicio:
            texto = "já teve Saúde conosco (desmarcado na plataforma): reconquista"
            saida.append({"texto": texto, "sinal": "+"})
            return saida + ([{"texto": f"LinkedIn: {p.nome.split()[0]} hoje está em {p.linkedin_empresa_atual}; confirmar o contato",
                              "sinal": "!"}] if p is not None and mudou_de_empresa(p) else [])
        if ultimo.status == "cancelado" or ultimo.fim_vigencia > date.today():
            texto = f"cancelou {ultimo.produto or VERTICAL_LABEL[vertical]} conosco: reconquista"
        else:
            texto = f"já teve {VERTICAL_LABEL[vertical]} conosco até {ultimo.fim_vigencia:%m/%Y}: reconquista"
        saida.append({"texto": texto, "sinal": "+"})
    if p is not None and mudou_de_empresa(p):
        saida.append({"texto": f"LinkedIn: {p.nome.split()[0]} hoje está em {p.linkedin_empresa_atual}; confirmar o contato",
                      "sinal": "!"})
    return saida


def linha(db: Session, n: Negocio, funis: dict, nomes: dict[str, str], hoje: date) -> dict:
    e, p = n.empresa, n.pessoa
    contato = p or melhor_contato(e)
    vigentes = seguros_vigentes(e)
    pot = potencial(n.vertical, e, contato, n)
    inf = influencia(contato)
    func, func_origem = _funcionarios(n, e)

    contatos = [c for c in (e.pessoas if e else []) if c.pipedrive_person_id]
    if p is not None and p.pipedrive_person_id and p not in contatos:
        contatos.insert(0, p)
    prox = _proxima_atividade(db, n.id)
    funil = funis.get(n.pipeline_id, {})
    return {
        "id": n.id, "pipedriveId": n.id_externo, "titulo": n.titulo, "produto": n.produto, "etapa": n.etapa,
        "funil": funil.get("nome"), "vertical": n.vertical, "valor": n.valor,
        "score": pot["score"], "criterios": pot["criterios"],
        "influencia": inf["score"], "influenciaComp": inf,
        "empresa": {"id": e.id, "nome": e.razao_social, "funcionarios": func, "funcionariosOrigem": func_origem}
        if e else None,
        "pessoa": {"id": contato.id, "nome": contato.nome, "cargo": contato.cargo, "temperatura": contato.temperatura,
                   "temperaturaMotivo": contato.temperatura_motivo, "linkedin": contato.linkedin_url,
                   "headline": contato.linkedin_headline, "doNegocio": contato is p} if contato else None,
        "contatos": [{"id": c.id, "nome": c.nome, "cargo": c.cargo, "area": area_pessoa(c)} for c in contatos],
        "quemDecide": quem_decide(db, e, n.vertical, nomes),
        "vigentes": [_vig_json(v) for v in vigentes],
        "saude": estado_saude(e, vigentes),
        "noticias": [{"titulo": x.titulo, "fonte": x.fonte, "url": x.url,
                      "data": x.publicada_em.date().isoformat() if x.publicada_em else None} for x in (e.noticias[:3] if e else [])],
        "motivos": _extras(e, p, n.vertical, vigentes) + motivos_potencial(pot), "dono": n.responsavel_email,
        "proximaAtividade": {"assunto": prox.assunto, "vencimento": prox.vencimento.isoformat(),
                             "responsavel": prox.responsavel.email} if prox else None,
    }


def interna(e: Empresa | None, settings: Settings) -> bool:
    """Organização da própria corretora (config: empresas_internas)."""
    if e is None:
        return False
    nome = e.nome_normalizado or ""
    return any(x and x in nome for x in settings.empresas_internas())


def montar(db: Session, settings: Settings, hoje: date | None = None) -> list[dict]:
    hoje = hoje or date.today()
    funis = settings.pipelines()
    ids = [pid for pid, f in funis.items() if f["tabela"]]
    nomes = {u.email: u.nome for u in db.scalars(select(Usuario))}
    negocios = db.scalars(select(Negocio).where(Negocio.fonte == "pipedrive", Negocio.status == "aberto",
                                                Negocio.pipeline_id.in_(ids))).all()
    linhas = [linha(db, n, funis, nomes, hoje) for n in negocios if not interna(n.empresa, settings)]
    # Empate no Potencial: maior influência, depois maior valor
    return sorted(linhas, key=lambda x: (x["score"], x["influencia"], x["valor"] or 0), reverse=True)


def negocios_abertos(db: Session, settings: Settings) -> list[dict]:
    """Negócios abertos nos funis das verticais (LF, RE, Saúde, Pipo; sem Canais Parceria), poucos campos e
    poucas consultas (a tabela antiga fazia várias consultas por negócio)."""
    funis = {pid: f for pid, f in settings.pipelines().items() if f["tabela"] and f["vertical"]}
    nomes = {u.email: u.nome for u in db.scalars(select(Usuario))}
    negocios = db.scalars(select(Negocio).where(Negocio.fonte == "pipedrive", Negocio.status == "aberto",
                                                Negocio.pipeline_id.in_(funis))
                          .options(selectinload(Negocio.empresa).selectinload(Empresa.negocios),
                                   selectinload(Negocio.pessoa))).all()
    proxima: dict[int, Atividade] = {}
    for a in db.scalars(select(Atividade).where(Atividade.negocio_id.in_([n.id for n in negocios]),
                                                Atividade.concluida.is_(False)).order_by(Atividade.vencimento, Atividade.id)):
        proxima.setdefault(a.negocio_id, a)
    saida = []
    for n in negocios:
        e = n.empresa
        if interna(e, settings):
            continue
        prox = proxima.get(n.id)
        saida.append({
            "id": n.id, "pipedriveId": n.id_externo, "titulo": n.titulo, "produto": n.produto or n.titulo,
            "funil": funis[n.pipeline_id]["nome"], "etapa": n.etapa, "vertical": n.vertical, "valor": n.valor,
            "dono": n.responsavel_email, "donoNome": nomes.get(n.responsavel_email or "", n.responsavel_email),
            "empresa": {"id": e.id, "nome": e.razao_social, "cliente": bool(seguros_vigentes(e)), "local": local(e),
                        "funcionarios": funcionarios_validos(e), "setor": setor_valido(e), "cidade": e.cidade,
                        "uf": e.uf, "cidadeFonte": e.cidade_fonte}
            if e else None,
            "pessoa": n.pessoa.nome if n.pessoa else None,
            "proximaAtividade": {"assunto": prox.assunto, "vencimento": prox.vencimento.isoformat()} if prox else None,
        })
    return sorted(saida, key=lambda x: ((x["empresa"] or {}).get("nome") or "").lower())


# Verticais que contam por produto: como achar o produto de um negócio, o encaixe de cada produto e o nome
POR_PRODUTO = {"linhas_financeiras": (produto_lf, produtos_lf, FITS),
               "ramos_elementares": (produto_re, produtos_re, FITS_RE)}


def produtos_da_empresa(e: Empresa, vigentes: list[Negocio], vertical: str = "linhas_financeiras") -> dict:
    """LF (E&O, D&O, Cyber, IMI; Garantia fora) e RE (Fiança, Patrimonial/Roubo, Transportes) por produto: sugere
    os que a empresa não tem nem negocia, com as etiquetas do que está em negociação e do que já foi ofertado e
    perdido. tipo "cross": a empresa ainda não tem nem negocia a vertical; "mais": mais produtos para quem já tem."""
    identificar, encaixes, nomes = POR_PRODUTO[vertical]
    da_vertical = [n for n in e.negocios if n.vertical == vertical]
    chave = lambda n: identificar(n.produto or n.titulo)  # noqa: E731
    tem = {chave(n) for n in vigentes if n.vertical == vertical} - {None}
    abertos = [n for n in da_vertical if n.status == "aberto"]
    negociando = {chave(n) for n in abertos} - {None}
    perdidos: dict[str, Negocio] = {}
    for n in da_vertical:
        k = chave(n)
        if n.status == "perdido" and k and k not in tem | negociando:
            if k not in perdidos or (n.perdido_em or date.min) > (perdidos[k].perdido_em or date.min):
                perdidos[k] = n
    excluir = tem | negociando
    todos = encaixes(e, excluir)
    sugeridos = [x for x in todos if x["sugerido"]] or todos[:1]
    etiquetas = [f"negociando {nomes[k][0]} agora" for k in sorted(negociando)]
    etiquetas += [f"{nomes[k][0]} ofertado em {n.perdido_em:%m/%Y}, perdido" if n.perdido_em
                  else f"{nomes[k][0]} já ofertado, perdido" for k, n in sorted(perdidos.items())]
    ja_tem = any(n.vertical == vertical for n in vigentes) or bool(abertos)
    return {"tipo": "mais" if ja_tem else "cross", "excluir": excluir, "etiquetas": etiquetas,
            "produtos": [{"chave": x["chave"], "produto": x["produto"], "valor": x["valor"], "texto": x["texto"]}
                         for x in sugeridos],
            "algumSugerido": any(x["sugerido"] for x in todos)}


def oportunidades(db: Session, settings: Settings, hoje: date | None = None) -> list[dict]:
    """Verticais que a empresa ainda não tem nem está negociando, para clientes (seguro vigente) e
    leads com algum card aberto no Pipedrive (negócio aberto com vertical, em qualquer funil).
    Também entram as empresas com negócio ganho nos últimos GANHOS_DIAS dias, mesmo sem nada aberto
    (`soGanho`: só aparecem no filtro "Ganhos recentes"). As outras ficam de fora.
    Ordenadas pelo Potencial na vertical da oportunidade."""
    hoje = hoje or date.today()
    funis = settings.pipelines()
    nomes = {u.email: u.nome for u in db.scalars(select(Usuario))}
    # Empresas com algum card aberto no Pipedrive ou ganho há pouco. Canais Parceria (sem vertical) são
    # parceiros, não leads de seguro: não contam
    desde = hoje - timedelta(days=GANHOS_DIAS)
    ids = select(Negocio.empresa_id).where(
        Negocio.empresa_id.is_not(None), Negocio.vertical.is_not(None), Negocio.fonte == "pipedrive",
        (Negocio.status == "aberto") | ((Negocio.status == "ganho") & (Negocio.ganho_em >= desde))).distinct()
    # Tudo de uma vez (antes eram milhares de consultas, uma por empresa, vertical e pessoa)
    empresas = db.scalars(select(Empresa).where(Empresa.id.in_(ids)).options(
        selectinload(Empresa.negocios), selectinload(Empresa.pessoas), selectinload(Empresa.noticias))).all()
    relacoes_de: dict[int, set[str]] = defaultdict(set)
    for empresa_id, email in db.execute(select(Interacao.empresa_id, Interacao.usuario_email)
                                        .where(Interacao.empresa_id.in_(ids))):
        relacoes_de[empresa_id].add(email)
    pontes = _pontes(db, ids)
    relacao = com_relacao(db, ids)
    nomes_id = {u.id: u.nome for u in db.scalars(select(Usuario))}
    # Incluídas à mão em Oportunidades e notas da equipe (a IA ajusta o Score pela nota mais recente avaliada)
    manuais = {(m.empresa_id, m.vertical): m for m in db.scalars(select(OportunidadeManual)
                                                                  .where(OportunidadeManual.empresa_id.in_(ids)))}
    notas_de: dict[tuple, list] = defaultdict(list)
    for n in db.scalars(select(NotaOportunidade).where(NotaOportunidade.empresa_id.in_(ids))
                        .order_by(NotaOportunidade.criada_em.desc(), NotaOportunidade.id.desc())):
        notas_de[(n.empresa_id, n.vertical)].append(n)
    estacionadas = {(x.empresa_id, x.vertical): x for x in db.scalars(
        select(OportunidadeEstacionada).where(OportunidadeEstacionada.empresa_id.in_(ids)))}
    # Próxima atividade aberta por empresa e vertical (a sem vertical vale para todas as verticais da empresa)
    proxima_de: dict[tuple, Atividade] = {}
    for a in db.scalars(select(Atividade).where(Atividade.empresa_id.in_(ids), Atividade.negocio_id.is_(None),
                                                Atividade.concluida.is_(False)).order_by(Atividade.vencimento, Atividade.id)):
        proxima_de.setdefault((a.empresa_id, a.vertical), a)
    saida = []
    for e in empresas:
        if interna(e, settings):
            continue
        abertos = [n for n in e.negocios if n.status == "aberto" and n.fonte == "pipedrive" and n.vertical]
        ganhos = sorted((n for n in e.negocios if n.status == "ganho" and n.fonte == "pipedrive" and n.vertical
                         and n.ganho_em and n.ganho_em >= desde), key=lambda n: n.ganho_em, reverse=True)
        if not abertos and not ganhos:
            continue
        vigentes = seguros_vigentes(e)
        tem = {v.vertical for v in vigentes}
        negociando = {n.vertical for n in e.negocios if n.status == "aberto" and n.vertical}
        # Quem da equipe troca e-mails com alguém da empresa (filtro "Relação de")
        relacoes = sorted(relacoes_de[e.id])
        for v in VERTICAIS:
            lf = produtos_da_empresa(e, vigentes, v) if v in POR_PRODUTO else None
            if lf is not None:
                if not lf["produtos"]:
                    continue  # já tem ou negocia todos os produtos da vertical
            elif v in tem or v in negociando:
                continue
            # RE: quem decide depende do produto (Fiança: Financeiro e Jurídico; Patrimonial e Transportes: Compras,
            # Facilities, Logística, Operações). Se a ponte já é dessa área, ela mesma é quem decide
            areas = (tuple(areas_re(x["chave"] for x in lf["produtos"])) or None) if v == "ramos_elementares" else None
            decide = quem_decide(db, e, v, nomes, pontes, relacao, areas=areas)
            alvo = db.get(Pessoa, decide["pessoas"][0]["id"]) if decide and decide["pessoas"] else None
            contato = alvo or melhor_contato(e)
            pot = potencial(v, e, contato, excluir=lf["excluir"] if lf else frozenset())
            notas = notas_de.get((e.id, v), [])
            nota = next((n for n in notas if n.ajuste is not None), None)
            if nota is not None and nota.ajuste:
                pot["score"] = round(max(0.0, min(100.0, pot["score"] + nota.ajuste)), 1)
            manual = manuais.get((e.id, v))
            parada, voltou = estacionada(e, estacionadas.get((e.id, v)), hoje, nomes_id)
            da_vertical = proxima_de.get((e.id, v))  # atividade aberta criada para esta oportunidade
            prox = da_vertical or proxima_de.get((e.id, None))
            contatos = [c for c in e.pessoas if c.pipedrive_person_id]
            func, func_origem = _funcionarios(None, e)
            saida.append({
                "id": f"{e.id}-{v}", "vertical": v, "verticalNome": VERTICAL_LABEL[v],
                "score": pot["score"], "criterios": pot["criterios"],
                "influencia": influencia(contato)["score"], "cliente": bool(vigentes),
                "empresa": {"id": e.id, "nome": e.razao_social, "funcionarios": func, "funcionariosOrigem": func_origem,
                            "noPipedrive": bool(e.pipedrive_org_id), "local": local(e), "cidade": e.cidade, "uf": e.uf,
                            "cidadeFonte": e.cidade_fonte, "setor": setor_valido(e)},
                "vigentes": [_vig_json(x) for x in vigentes], "saude": estado_saude(e, vigentes),
                # Responsáveis pelos cards abertos da empresa (resumo de pendências por responsável)
                "donos": sorted({x.responsavel_email for x in abertos if x.responsavel_email}),
                # Negócios ganhos nos últimos GANHOS_DIAS dias (filtro "Ganhos recentes"), com quem fechou
                "ganhos": ganhos_por_produto(ganhos, nomes),
                "soGanho": not abertos,
                "noticias": [{"titulo": x.titulo, "fonte": x.fonte, "url": x.url, "portal": portal(x.site),
                              "data": x.publicada_em.date().isoformat() if x.publicada_em else None} for x in e.noticias[:2]],
                "relacoes": relacoes,
                "negociando": [{"vertical": x.vertical, "produto": x.produto or x.titulo, "etapa": x.etapa,
                                "funil": funis.get(x.pipeline_id, {}).get("nome")} for x in abertos],
                "quemDecide": decide,
                "motivos": ([{"texto": f"atualização da equipe ({nota.ajuste:+d}): {nota.motivo}", "sinal": "+" if nota.ajuste > 0 else "-"}]
                            if nota is not None and nota.ajuste else []) + _extras(e, None, v, vigentes) + motivos_potencial(pot),
                "notas": [{"id": n.id, "texto": n.texto, "autor": nomes_id.get(n.usuario_id), "em": n.criada_em.isoformat(timespec="minutes"),
                           "ajuste": n.ajuste, "motivo": n.motivo} for n in notas],
                "contatos": [{"id": c.id, "nome": c.nome, "cargo": c.cargo, "area": area_pessoa(c)} for c in contatos],
                "proximaAtividade": {"id": prox.id, "assunto": prox.assunto, "tipo": prox.tipo,
                                     "vencimento": prox.vencimento.isoformat(), "responsavel": prox.responsavel.email,
                                     "responsavelNome": prox.responsavel.nome} if prox else None,
                # Funil: nao_atuamos | em_foco (atividade aberta) | atualizado (atualização ainda valendo)
                **estagio(notas[0] if notas else None, prox, hoje),
                "faltando": (falta := faltando(v, e, decide, any(tem_relacao(p, relacao) for p in e.pessoas),
                                               sem_produto=bool(lf) and not lf["algumSugerido"])),
                # Incluída à mão (antes da reunião de sexta) ou com atividade aberta para ela (alguém já está
                # trabalhando): entra mesmo faltando algo, com o que ainda falta à mostra
                "analisada": not falta or manual is not None or da_vertical is not None,
                "porAtividade": bool(falta) and manual is None and da_vertical is not None,
                "manual": {"por": nomes_id.get(manual.usuario_id), "em": manual.criada_em.isoformat(timespec="minutes")}
                if manual else None,
                # Estacionada (fora da tela por ora) ou que voltou da lista de estacionadas, e por quê
                "estacionada": parada, "voltou": voltou,
                "praca": praca(e) if v == "saude" else None,
                # LF e RE: "cross" (a empresa ainda não tem nem negocia a vertical) ou "mais" (mais produtos para quem
                # já tem); produtos sugeridos e etiquetas ("negociando D&O agora", "Cyber ofertado em 03/2026, perdido")
                "tipo": lf["tipo"] if lf else "cross",
                "produtos": lf["produtos"] if lf else [],
                "etiquetas": lf["etiquetas"] if lf else [],
            })
    por_id = {e.id: e for e in empresas}
    for item in saida:
        item["pendencias"] = pendencias(item)
        item["pesquisa"] = pesquisa(item, por_id[item["empresa"]["id"]], settings.linkedin_validade_dias)
    return sorted(saida, key=lambda x: (x["score"], x["influencia"]), reverse=True)


ATUALIZACAO_DIAS = 61  # atualização antiga, sem validade escolhida: vale 2 meses


def estagio(nota: NotaOportunidade | None, prox: Atividade | None, hoje: date) -> dict:
    """Estágio no funil de Oportunidades. Atualizado: a última atualização do negócio ainda vale (a equipe escolhe
    por quantos meses ao escrever). Em foco: tem atividade aberta (criada depois de a atualização vencer, se houver).
    Não atuamos: o resto, inclusive quem tinha atualização vencida e nenhuma atividade nova."""
    vale = (nota.vale_ate or (nota.criada_em.date() + timedelta(days=ATUALIZACAO_DIAS))) if nota else None
    if vale and vale >= hoje:
        return {"estagio": "atualizado", "atualizadoAte": vale.isoformat()}
    if prox is not None and (vale is None or prox.criada_em.date() >= vale):
        return {"estagio": "em_foco", "atualizadoAte": vale.isoformat() if vale else None}
    return {"estagio": "nao_atuamos", "atualizadoAte": vale.isoformat() if vale else None}


def estacionada(e: Empresa, x: OportunidadeEstacionada | None, hoje: date,
                nomes_id: dict[int, str]) -> tuple[dict | None, str | None]:
    """(estacionada, voltou). Volta para a tela quando chega a data de rever ou, antes disso, com notícia de M&A ou
    aporte ou negócio ganho depois de estacionar (o registro fica e a linha ganha a etiqueta do motivo da volta)."""
    if x is None:
        return None, None
    desde = x.criada_em
    if x.rever_em and x.rever_em <= hoje:
        return None, f"chegou a data de rever ({x.rever_em:%d/%m/%Y})"
    if any((n.publicada_em or n.coletada_em) and (n.publicada_em or n.coletada_em) > desde for n in noticias_ma(e)):
        return None, "notícia de M&A ou aporte depois de estacionar"
    if any(n.status == "ganho" and n.ganho_em and n.ganho_em > desde.date() for n in e.negocios):
        return None, "negócio ganho depois de estacionar"
    return {"por": nomes_id.get(x.usuario_id), "em": x.criada_em.isoformat(timespec="minutes"), "motivo": x.motivo,
            "motivoTexto": MOTIVOS_ESTACIONAR.get(x.motivo, x.motivo), "texto": x.texto,
            "reverEm": x.rever_em.isoformat() if x.rever_em else None}, None


def ganhos_por_produto(ganhos: list[Negocio], nomes: dict[str, str]) -> list[dict]:
    """Um item por produto ganho (vários negócios do mesmo produto, ex.: três Garantias, viram um só, com `qtd`),
    com o ganho mais recente: data e quem fechou. `ganhos` já vem do mais recente para o mais antigo."""
    por_produto: dict[str, dict] = {}
    for x in ganhos:
        produto = x.produto or x.titulo or ""
        item = por_produto.setdefault(" ".join(sem_acento(produto).lower().split()), {
            "vertical": x.vertical, "produto": produto, "em": x.ganho_em.isoformat(), "qtd": 0,
            "dono": x.responsavel_email, "donoNome": nomes.get(x.responsavel_email, x.responsavel_email)})
        item["qtd"] += 1
    return list(por_produto.values())


def _e_ponte(p: dict | None) -> bool:
    return bool(p) and (p.get("relacao", 0) >= 20 or bool(p.get("ponteManual")))


def pendencias(o: dict) -> list[dict]:
    """Só os campos vazios que a equipe pode preencher à mão numa oportunidade já na tela (aba "Completar dados").
    O que já foi capturado não entra. Ponte: com alguém que troca e-mails com a equipe (ou marcado à mão), só o cargo
    dele, se faltar; sem ninguém, marcar a ponte. Cada item: {campo, texto, pessoa?}."""
    if not o["analisada"]:
        return []
    d, emp = o.get("quemDecide") or {}, o["empresa"]
    falta = " ".join(o["faltando"]) if o.get("manual") else ""
    saida = []
    if (d.get("pessoas") and not d.get("daArea")) or "quem decide" in falta:
        saida.append({"campo": "quem_decide",
                      "texto": f"quem decide de {' / '.join(d.get('areas') or [])}" if d.get("areas") else "quem decide"})
    ponte = next((p for p in [*(d.get("pessoas") or []), d.get("ponte")] if _e_ponte(p)), None)
    if ponte is None:
        saida.append({"campo": "ponte", "texto": "ponte (ninguém troca e-mails com a equipe)"})
    elif not ponte.get("cargo"):
        saida.append({"campo": "cargo", "texto": f"cargo de {ponte['nome']}",
                      "pessoa": {"id": ponte["id"], "nome": ponte["nome"]}})
    for campo, texto, ok in (("cidade", "cidade", emp.get("local") and "cidade" not in falta and "praça" not in falta),
                             ("funcionarios", "funcionários", emp.get("funcionarios")),
                             ("setor", "setor", emp.get("setor"))):
        if not ok:
            saida.append({"campo": campo, "texto": texto})
    return saida


# Etapas da pesquisa de cada vertical (barrinha em Oportunidades): rótulo na tela e o nome em `faltando`
ETAPAS_BASE = {
    "saude": (("cidade e praça", ("cidade", "praça")), ("funcionários", ("funcionários",)), ("setor", ("setor",))),
    "linhas_financeiras": (("Receita Federal", ("Receita",)), ("notícias", ("notícias",)),
                           ("site lido pela IA", ("site (IA)",)), ("cargos-chave no LinkedIn", ("cargos-chave",))),
    "ramos_elementares": (("setor", ("setor",)), ("funcionários", ("funcionários",))),
}
# A lista de quem decide pelo Sales Navigator, por vertical (chave em Empresa.linkedin_areas)
LISTA_PESSOAS = {"saude": PESSOAS, "linhas_financeiras": PESSOAS_LF, "ramos_elementares": PESSOAS_RE}
# O que a plataforma busca sozinha mesmo sem LinkedIn
SEM_LINKEDIN = {"Receita Federal", "notícias", "site lido pela IA"}


def pesquisa(o: dict, e: Empresa, validade_dias: int = 180, agora: datetime | None = None) -> dict:
    """Quanto da pesquisa da oportunidade está feito (barrinha em Oportunidades): os dados da vertical, quem decide
    da área e a ponte por e-mail (com cargo). Preenchido à mão conta como feito. Cada etapa pendente diz quem vai
    resolver: a plataforma (ainda vai buscar sozinha) ou a equipe (só à mão, em Completar dados).
    Estado: "completo" (100%), "pesquisando" (a plataforma ainda busca algo) ou "equipe" (o resto depende da equipe).
    Quem decide já procurado no LinkedIn sem achar ninguém diz quando foi e quando será a nova busca (`dica`)."""
    agora = agora or datetime.utcnow()
    falta = set(o["faltando"])
    areas = e.linkedin_areas or {}
    # Sem página no LinkedIn, o que viria de lá só chega à mão
    sem_linkedin = e.linkedin_nao_encontrado and not e.linkedin_url
    etapas = []
    for texto, chaves in ETAPAS_BASE.get(o["vertical"], ()):
        ok = not falta.intersection(chaves)
        quem = "plataforma" if texto in SEM_LINKEDIN or not sem_linkedin else "equipe"
        etapas.append({"texto": texto, "ok": ok, "quem": None if ok else quem})
    d = o.get("quemDecide") or {}
    area = " / ".join(d.get("areas") or [])
    if d.get("daArea"):
        etapas.append({"texto": f"quem decide ({area})" if area else "quem decide", "ok": True, "quem": None})
    else:
        etapa = {"texto": f"quem decide ({area})" if area else "quem decide", "ok": False, "quem": "plataforma"}
        lida = areas.get(LISTA_PESSOAS.get(o["vertical"], ""))
        if sem_linkedin:
            etapa |= {"quem": "equipe", "dica": "empresa sem LinkedIn; só por e-mail, Pipedrive ou à mão"}
        elif lida:
            lida = datetime.fromisoformat(lida)
            nova = lida + timedelta(days=validade_dias)
            if nova > agora:  # já procurado e sem ninguém da área: até a nova busca, só à mão
                etapa |= {"quem": "equipe", "dica": f"LinkedIn procurado em {lida:%d/%m/%Y} sem achar ninguém; "
                                                     f"nova busca em {nova:%d/%m/%Y}, ou preencha à mão"}
        etapas.append(etapa)
    campos = {x["campo"] for x in o.get("pendencias") or []}
    etapas.append({"texto": "ponte por e-mail", "ok": "ponte" not in campos, "quem": "equipe" if "ponte" in campos else None})
    if "ponte" not in campos:
        etapas.append({"texto": "cargo da ponte", "ok": "cargo" not in campos, "quem": "equipe" if "cargo" in campos else None})
    feitas = sum(x["ok"] for x in etapas)
    pendentes = {x["quem"] for x in etapas if not x["ok"]}
    estado = "completo" if not pendentes else "pesquisando" if "plataforma" in pendentes else "equipe"
    return {"pct": round(100 * feitas / len(etapas)), "estado": estado, "etapas": etapas}


def semana(ref: date) -> tuple[date, date]:
    """Segunda a sexta da semana de `ref` (a reunião é na sexta)."""
    seg = ref - timedelta(days=ref.weekday())
    return seg, seg + timedelta(days=4)


PERIODOS_TODOS = ("semana", "fazer", "vencido", "hoje", "amanha", "proxima", "periodo")


def todos(db: Session, ref: date | None = None, periodo: str = "semana", de: date | None = None,
          ate: date | None = None, responsavel_id: int | None = None) -> dict:
    """Atividades criadas pela plataforma, como os filtros do Pipedrive. semana (padrão, a da reunião de sexta):
    pendentes até sexta, inclusive atrasadas, e as feitas na semana de `ref`. fazer: todas as pendentes. vencido:
    pendentes com vencimento antes de hoje. hoje, amanha, proxima (próxima semana) e periodo (de/ate): pendentes e
    feitas com vencimento no intervalo. responsavel_id: só as de quem executa."""
    hoje = date.today()
    ref = ref or hoje
    q = select(Atividade).order_by(Atividade.vencimento, Atividade.id)
    if responsavel_id:
        q = q.where(Atividade.responsavel_id == responsavel_id)
    pend = q.where(Atividade.concluida.is_(False))
    ini = fim = None
    if periodo == "fazer":
        itens = db.scalars(pend).all()
    elif periodo == "vencido":
        itens = db.scalars(pend.where(Atividade.vencimento < hoje)).all()
    elif periodo in ("hoje", "amanha", "proxima", "periodo"):
        if periodo == "hoje":
            ini = fim = hoje
        elif periodo == "amanha":
            ini = fim = hoje + timedelta(days=1)
        elif periodo == "proxima":
            ini = semana(hoje)[0] + timedelta(days=7)
            fim = ini + timedelta(days=6)
        else:
            ini, fim = de or hoje, ate or de or hoje
        itens = db.scalars(q.where(Atividade.vencimento >= ini, Atividade.vencimento <= fim)).all()
    else:
        ini, fim = semana(ref)
        itens = [*db.scalars(pend.where(Atividade.vencimento <= fim)),
                 *db.scalars(q.where(Atividade.concluida.is_(True), Atividade.vencimento >= ini,
                                     Atividade.vencimento <= fim))]
    return {"periodo": periodo if periodo in PERIODOS_TODOS else "semana",
            "inicio": ini.isoformat() if ini else None, "fim": fim.isoformat() if fim else None,
            "itens": [item_atividade(a, hoje) for a in itens]}


TIERS_SAUDE = ("A+", "A", "B", "C")


def _nota_cliente(n, nomes_id: dict[int, str]) -> dict:
    """Atualização do negócio ou oportunidade estacionada, no histórico do cliente."""
    base = {"vertical": n.vertical, "verticalNome": VERTICAL_LABEL.get(n.vertical, n.vertical),
            "autor": nomes_id.get(n.usuario_id), "em": n.criada_em.isoformat(timespec="minutes")}
    if isinstance(n, OportunidadeEstacionada):
        rever = f" · rever em {n.rever_em:%d/%m/%Y}" if n.rever_em else ""
        return {**base, "id": f"e{n.id}", "estacionada": True, "ajuste": None,
                "texto": f"Estacionada: {MOTIVOS_ESTACIONAR.get(n.motivo, n.motivo)}" + (f" ({n.texto})" if n.texto else ""),
                "motivo": f"fora da tela Oportunidades{rever}"}
    return {**base, "id": n.id, "texto": n.texto, "ajuste": n.ajuste, "motivo": n.motivo}


def clientes(db: Session, settings: Settings) -> list[dict]:
    """Aba Clientes: empresas com seguro vigente (negócio ganho com fim de vigência depois de hoje; Saúde, que não tem
    fim, vale até ser desmarcado), para o time dar o tier de Saúde à mão. Produtos sem repetir, funcionários de onde
    houver (à mão, LinkedIn ou Pipedrive), com quem a equipe fala (ponte por e-mail ou marcada à mão, com o cargo) e
    todas as atualizações do negócio da empresa, de todas as verticais."""
    ids = select(Negocio.empresa_id).where(Negocio.empresa_id.is_not(None),
                                           (Negocio.status == "ganho") | Negocio.fonte.in_(("manual", "zeca"))).distinct()
    empresas = db.scalars(select(Empresa).where(Empresa.id.in_(ids)).options(
        selectinload(Empresa.negocios), selectinload(Empresa.pessoas))).all()
    empresas = [e for e in empresas if not interna(e, settings) and seguros_vigentes(e)]
    if not empresas:
        return []
    alvo = [e.id for e in empresas]
    nomes = {u.email: u.nome for u in db.scalars(select(Usuario))}
    nomes_id = {u.id: u.nome for u in db.scalars(select(Usuario))}
    pontes, relacao = _pontes(db, alvo), com_relacao(db, alvo)
    notas_de: dict[int, list] = defaultdict(list)
    for n in db.scalars(select(NotaOportunidade).where(NotaOportunidade.empresa_id.in_(alvo))
                        .order_by(NotaOportunidade.criada_em.desc(), NotaOportunidade.id.desc())):
        notas_de[n.empresa_id].append(n)
    for x in db.scalars(select(OportunidadeEstacionada).where(OportunidadeEstacionada.empresa_id.in_(alvo))):
        notas_de[x.empresa_id].append(x)
    saida = []
    for e in empresas:
        notas_de[e.id].sort(key=lambda n: n.criada_em, reverse=True)
        produtos: dict[str, dict] = {}
        for v in seguros_vigentes(e):
            produto = (v.produto or v.titulo or VERTICAL_LABEL.get(v.vertical, "")).strip()
            chave = " ".join(sem_acento(produto).lower().split())
            atual = produtos.get(chave)
            fim = v.fim_vigencia.isoformat() if v.fim_vigencia else None
            if atual is None:
                # Saúde (sem fim de vigência) pode ser desmarcado na tela, menos o da planilha do Zeca
                produtos[chave] = {"vertical": v.vertical, "produto": produto, "fim": fim, "qtd": 1,
                                   "desmarcavel": v.vertical == "saude" and v.fonte != "zeca"}
            else:  # o mesmo produto em dois negócios vira um só (com o fim mais distante)
                atual["qtd"] += 1
                if fim and (atual["fim"] is None or fim > atual["fim"]):
                    atual["fim"] = fim
        contatos = sorted((p for p in e.pessoas if tem_relacao(p, relacao)),
                          key=lambda p: (-(p.score_relacionamento or 0), p.nome))
        # Critérios do Score de Saúde (localização, qualificação, funcionários, RH), para o time completar à mão
        pot = potencial("saude", e, None)
        rh = [p for p in e.pessoas if area_pessoa(p) == "rh"]
        saida.append({
            "id": e.id, "nome": e.razao_social, "local": local(e),
            "produtos": sorted(produtos.values(), key=lambda x: (x["vertical"], x["produto"])),
            "funcionarios": e.funcionarios, "funcionariosOrigem": FONTE_FUNC.get(e.funcionarios_fonte or "", ""),
            "cidade": e.cidade, "uf": e.uf, "cidadeFonte": e.cidade_fonte,
            "setor": setor_valido(e), "setorFonte": e.setor_fonte,
            "rh": [{"id": p.id, "nome": p.nome, "cargo": p.cargo or p.linkedin_headline} for p in rh],
            "saude": {"score": pot["score"],
                      "criterios": {c["nome"]: {"valor": c["valor"], "texto": c["texto"], "peso": c["peso"]}
                                    for c in pot["criterios"]}},
            "tier": e.tier_saude, "tierPor": nomes_id.get(e.tier_saude_por_id),
            "tierEm": e.tier_saude_em.isoformat(timespec="minutes") if e.tier_saude_em else None,
            "contatos": [{"id": p.id, "nome": p.nome, "cargo": p.cargo or p.linkedin_headline,
                          "quemFala": nomes.get(pontes.get(p.id), pontes.get(p.id)), "ponteManual": p.ponte_manual}
                         for p in contatos],
            "pessoas": [{"id": p.id, "nome": p.nome, "cargo": p.cargo} for p in sorted(e.pessoas, key=lambda p: p.nome)],
            "notas": [_nota_cliente(n, nomes_id) for n in notas_de[e.id]],
        })
    return sorted(saida, key=lambda x: sem_acento(x["nome"]).lower())


# Horário de Brasília (sem horário de verão desde 2019): as datas no banco são UTC
FUSO_BRASILIA = timedelta(hours=-3)


def log_semana(db: Session, u: Usuario, ref: date | None = None) -> dict:
    """O que a pessoa fez na semana de `ref` (segunda a domingo, horário de Brasília), numa linha do tempo para a
    reunião de sexta: atividades criadas e concluídas, atualizações do negócio, oportunidades incluídas à mão e o que
    ela completou à mão (`Registro`). Só o trabalho de cross sell: negócios ganhos no Pipedrive não entram.
    No fim, as atividades dela ainda atrasadas."""
    ref = ref or (datetime.utcnow() + FUSO_BRASILIA).date()
    seg = ref - timedelta(days=ref.weekday())
    de = datetime.combine(seg, datetime.min.time()) - FUSO_BRASILIA  # em UTC, como no banco
    ate = de + timedelta(days=7)
    itens = []

    def item(em, tipo, texto, empresa=None, vertical=None, detalhe=None):
        local = em + FUSO_BRASILIA
        itens.append({"_em": em, "em": local.isoformat(timespec="minutes"), "dia": local.date().isoformat(), "tipo": tipo,
                      "texto": texto, "detalhe": detalhe, "empresa": empresa.razao_social if empresa else None,
                      "empresaId": empresa.id if empresa else None, "vertical": vertical,
                      "verticalNome": VERTICAL_LABEL.get(vertical) if vertical else None})

    def empresa_de(a: Atividade):
        return a.empresa or (a.negocio.empresa if a.negocio else None)

    def vertical_de(a: Atividade):
        return a.vertical or (a.negocio.vertical if a.negocio else None)

    for a in db.scalars(select(Atividade).where(Atividade.criada_por_id == u.id, Atividade.criada_em >= de,
                                                Atividade.criada_em < ate)):
        para = f"para {a.responsavel.nome} · " if a.responsavel_id != u.id else ""
        item(a.criada_em, "criou", f"criou a atividade «{a.assunto}»", empresa_de(a), vertical_de(a),
             f"{para}vence {a.vencimento:%d/%m}" + (f" · {a.nota}" if a.nota else ""))
    for a in db.scalars(select(Atividade).where(Atividade.responsavel_id == u.id, Atividade.concluida.is_(True),
                                                Atividade.concluida_em >= de, Atividade.concluida_em < ate)):
        item(a.concluida_em, "concluiu", f"concluiu «{a.assunto}»", empresa_de(a), vertical_de(a), a.nota)
    for n in db.scalars(select(NotaOportunidade).where(NotaOportunidade.usuario_id == u.id,
                                                       NotaOportunidade.criada_em >= de, NotaOportunidade.criada_em < ate)):
        efeito = f"Score {n.ajuste:+d}: {n.motivo}" if n.ajuste else ("sem efeito no Score" if n.ajuste == 0 else None)
        item(n.criada_em, "atualizacao", f"atualização: {n.texto}", db.get(Empresa, n.empresa_id), n.vertical, efeito)
    for m in db.scalars(select(OportunidadeManual).where(OportunidadeManual.usuario_id == u.id,
                                                         OportunidadeManual.criada_em >= de,
                                                         OportunidadeManual.criada_em < ate)):
        item(m.criada_em, "incluiu", "incluiu à mão em Oportunidades", db.get(Empresa, m.empresa_id), m.vertical)
    for r in db.scalars(select(Registro).where(Registro.usuario_id == u.id, Registro.em >= de, Registro.em < ate)):
        item(r.em, r.tipo, r.texto, r.empresa)
    itens.sort(key=lambda x: x.pop("_em"))
    hoje = (datetime.utcnow() + FUSO_BRASILIA).date()
    atrasadas = db.scalars(select(Atividade).where(Atividade.responsavel_id == u.id, Atividade.concluida.is_(False),
                                                   Atividade.vencimento < hoje).order_by(Atividade.vencimento)).all()
    contar = Counter(x["tipo"] for x in itens)
    return {"inicio": seg.isoformat(), "fim": (seg + timedelta(days=4)).isoformat(),
            "usuario": {"id": u.id, "nome": u.nome, "email": u.email},
            "resumo": {"concluidas": contar["concluiu"], "atualizacoes": contar["atualizacao"], "criadas": contar["criou"],
                       "dados": sum(v for k, v in contar.items() if k not in ("concluiu", "atualizacao", "criou")),
                       "empresas": len({x["empresaId"] for x in itens if x["empresaId"]})},
            "itens": itens, "atrasadas": [item_atividade(a, hoje) for a in atrasadas]}


def item_atividade(a: Atividade, ref: date) -> dict:
    return {"id": a.id, "assunto": a.assunto, "tipo": a.tipo, "vencimento": a.vencimento.isoformat(),
            "atrasada": not a.concluida and a.vencimento < ref, "concluida": a.concluida,
            "responsavel": a.responsavel.email, "responsavelNome": a.responsavel.nome,
            "empresa": a.empresa.razao_social if a.empresa else None, "empresaId": a.empresa_id,
            "pessoa": a.pessoa.nome if a.pessoa else None, "negocio": a.negocio.titulo if a.negocio else None,
            "negocioId": a.negocio_id, "vertical": (v := a.vertical or (a.negocio.vertical if a.negocio else None)),
            "verticalNome": VERTICAL_LABEL.get(v) if v else None,
            "etapa": a.negocio.etapa if a.negocio else None, "valor": a.negocio.valor if a.negocio else None,
            "nota": a.nota}



# --- Diário da análise ---------------------------------------------------------------------------------------------
# Passos da rotina que contam como trabalho de análise no diário (fonte do SyncLog -> rótulo)
TRABALHO_DIA = {"receita": "Receita", "noticias": "Notícias", "sites-ia": "Sites lidos pela IA",
                "linkedin-sites": "LinkedIn achado no site", "cnpj-sites": "CNPJ achado no site", "email": "E-mails"}


def hoje_brasilia() -> date:
    return (datetime.utcnow() + FUSO_BRASILIA).date()


def registrar_dia(db: Session, settings: Settings) -> dict:
    """Foto da análise de hoje (a última rodada do dia vale): por vertical, as empresas analisadas, quantas na fila
    e o que falta na fila. Estacionadas e "só ganho" não contam; os casos definitivos (fora da praça, MEI) também não."""
    ops = [o for o in oportunidades(db, settings) if o["vertical"] in VERTICAIS_OPORTUNIDADES
           and not o["soGanho"] and not o["estacionada"]]
    dia, saida = hoje_brasilia(), {}
    for v in VERTICAIS_OPORTUNIDADES:
        dav = [o for o in ops if o["vertical"] == v]
        ids = sorted({o["empresa"]["id"] for o in dav if o["analisada"]})
        fila = [o for o in dav if not o["analisada"] and not set(o["faltando"]) & set(FINAIS)]
        falta = Counter(f for o in fila for f in o["faltando"])
        x = db.scalar(select(AnaliseDia).where(AnaliseDia.data == dia, AnaliseDia.vertical == v))
        if x is None:
            x = AnaliseDia(data=dia, vertical=v)
            db.add(x)
        x.analisadas, x.fila, x.ids, x.faltando = len(ids), len({o["empresa"]["id"] for o in fila}), ids, dict(falta)
        x.atualizado_em = datetime.utcnow()
        saida[v] = len(ids)
    db.commit()
    return saida


def diario(db: Session, dias: int = 14) -> dict:
    """Diário da análise: por dia (horário de Brasília), quantas oportunidades estavam analisadas e na fila em cada
    vertical, quem entrou em Oportunidades naquele dia, e o trabalho da rotina (pedidos ao LinkedIn, Receita,
    notícias, sites). E o que mais trava a fila hoje."""
    fim = hoje_brasilia()
    inicio = fim - timedelta(days=dias - 1)
    fotos = db.scalars(select(AnaliseDia).where(AnaliseDia.data >= inicio - timedelta(days=1))
                       .order_by(AnaliseDia.data)).all()
    por_dia: dict[date, dict[str, AnaliseDia]] = defaultdict(dict)
    for f in fotos:
        por_dia[f.data][f.vertical] = f
    de = datetime.combine(inicio, datetime.min.time()) - FUSO_BRASILIA
    dia_de = lambda t: (t + FUSO_BRASILIA).date()  # noqa: E731
    linkedin: dict[date, Counter] = defaultdict(Counter)
    for p in db.scalars(select(LinkedinPedido).where(LinkedinPedido.criado_em >= de)):
        linkedin[dia_de(p.criado_em)]["pedidos"] += 1
        linkedin[dia_de(p.criado_em)][f"pedidos:{p.vertical or 'geral'}"] += 1
        if p.situacao in ("erro", "expirado"):
            linkedin[dia_de(p.criado_em)]["erros"] += 1
    for p in db.scalars(select(LinkedinPedido).where(LinkedinPedido.concluido_em >= de,
                                                     LinkedinPedido.situacao == "aplicado")):
        linkedin[dia_de(p.concluido_em)]["aplicados"] += 1
    trabalho: dict[date, Counter] = defaultdict(Counter)
    falhas: dict[date, list] = defaultdict(list)
    for x in db.scalars(select(SyncLog).where(SyncLog.inicio >= de, SyncLog.fonte.in_(list(TRABALHO_DIA)))):
        trabalho[dia_de(x.inicio)][x.fonte] += x.registros or 0
        if x.erro:
            falhas[dia_de(x.inicio)].append(TRABALHO_DIA[x.fonte])
    preenchido: dict[date, Counter] = defaultdict(Counter)
    empresas_dia: dict[date, set] = defaultdict(set)
    for em, fonte, empresa_id in db.execute(select(AlteracaoDado.em, AlteracaoDado.fonte, AlteracaoDado.empresa_id)
                                            .where(AlteracaoDado.em >= de)):
        preenchido[dia_de(em)][fonte] += 1
        if empresa_id:
            empresas_dia[dia_de(em)].add(empresa_id)
    entraram_ids: set[int] = set()
    linhas = []
    d = fim
    while d >= inicio:
        hoje_f, ontem_f = por_dia.get(d, {}), por_dia.get(d - timedelta(days=1), {})
        verts, entraram = {}, []
        for v in VERTICAIS_OPORTUNIDADES:
            f, a = hoje_f.get(v), ontem_f.get(v)
            if f is None:
                continue
            novos = sorted(set(f.ids) - set(a.ids)) if a is not None else []
            entraram_ids.update(novos)
            entraram += [(v, i) for i in novos]
            verts[v] = {"analisadas": f.analisadas, "fila": f.fila, "entraram": len(novos),
                        "variacao": f.analisadas - a.analisadas if a is not None else None}
        lk, tr = linkedin.get(d, Counter()), trabalho.get(d, Counter())
        linhas.append({"data": d.isoformat(), "verticais": verts, "entraram": entraram,
                       "linkedin": {"pedidos": lk["pedidos"], "aplicados": lk["aplicados"], "erros": lk["erros"],
                                    "porVertical": {v: lk[f"pedidos:{v}"] for v in VERTICAIS_OPORTUNIDADES if lk[f"pedidos:{v}"]}},
                       "trabalho": {TRABALHO_DIA[k]: n for k, n in tr.items() if n},
                       "preenchido": {"total": sum(preenchido.get(d, Counter()).values()),
                                      "empresas": len(empresas_dia.get(d, ())),
                                      "porFonte": _por_fonte(preenchido.get(d, Counter()))},
                       "falhas": sorted(set(falhas.get(d, [])))})
        d -= timedelta(days=1)
    nomes = {e.id: e.razao_social for e in db.scalars(select(Empresa).where(Empresa.id.in_(entraram_ids)))} if entraram_ids else {}
    for linha in linhas:
        linha["entraram"] = [{"vertical": v, "verticalNome": VERTICAL_LABEL[v], "id": i, "nome": nomes.get(i, "?")}
                             for v, i in linha["entraram"]]
    atual = {v: por_dia.get(max(por_dia), {}).get(v) for v in VERTICAIS_OPORTUNIDADES} if por_dia else {}
    travas = {v: sorted(({"motivo": k, "empresas": n} for k, n in (f.faltando or {}).items()), key=lambda x: -x["empresas"])
              for v, f in atual.items() if f is not None}
    return {"dias": linhas, "travas": travas, "verticais": {v: VERTICAL_LABEL[v] for v in VERTICAIS_OPORTUNIDADES},
            "desde": min(por_dia).isoformat() if por_dia else None}


def _por_fonte(c: Counter) -> dict[str, int]:
    from crosssell.historico import ORIGENS

    saida: Counter = Counter()
    for fonte, n in c.items():
        saida[ORIGENS.get(fonte, fonte)] += n
    return dict(saida.most_common())


def preenchido_no_dia(db: Session, dia: date) -> dict:
    """Tudo o que a plataforma preencheu no dia (horário de Brasília), por empresa: campo, antes, depois e a fonte."""
    from crosssell.historico import ORIGENS

    de = datetime.combine(dia, datetime.min.time()) - FUSO_BRASILIA
    linhas = db.scalars(select(AlteracaoDado).where(AlteracaoDado.em >= de, AlteracaoDado.em < de + timedelta(days=1))
                        .order_by(AlteracaoDado.empresa_id, AlteracaoDado.em, AlteracaoDado.id)).all()
    nomes = {e.id: e.razao_social for e in db.scalars(select(Empresa).where(
        Empresa.id.in_({x.empresa_id for x in linhas if x.empresa_id})))} if linhas else {}
    por_empresa: dict[int | None, list] = defaultdict(list)
    for x in linhas:
        por_empresa[x.empresa_id].append({"campo": x.campo, "antes": x.antes, "depois": x.depois,
                                          "fonte": ORIGENS.get(x.fonte, x.fonte),
                                          "hora": (x.em + FUSO_BRASILIA).strftime("%H:%M")})
    empresas = [{"id": eid, "nome": nomes.get(eid, "(sem empresa)"), "itens": itens} for eid, itens in por_empresa.items()]
    return {"data": dia.isoformat(), "total": len(linhas),
            "empresas": sorted(empresas, key=lambda x: (-len(x["itens"]), sem_acento(x["nome"]).lower()))}
