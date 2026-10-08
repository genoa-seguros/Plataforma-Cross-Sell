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
from crosssell.connectors.linkedin import FUNCIONARIOS, PESSOAS_LF, mudou_de_empresa
from crosssell.connectors.noticias import portal
from crosssell.site_ia import endereco
from crosssell.models import Atividade, Empresa, Interacao, Negocio, Pessoa, Usuario
from crosssell.normalize import AREA_LABEL, classificar_area
from crosssell.potencial import (FITS, faltando_saude, funcionarios_validos, influencia, mei, melhor_contato,
                                 motivos as motivos_potencial, potencial, praca, produto_lf, produtos_lf, setor_valido)

# Verticais que já têm fluxo de análise definido e aparecem em Oportunidades (RE entra quando o dela for definido)
VERTICAIS_OPORTUNIDADES = ("saude", "linhas_financeiras")
# Nunca entram em Oportunidades, nem na fila de análise
FINAIS = ("fora da praça", "MEI")


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


def faltando(vertical: str, e: Empresa | None, decide: dict | None = None, tem_ponte: bool = False) -> list[str]:
    """O que falta para a empresa entrar em Oportunidades na vertical (vazio = analisada).
    Saúde: praça alvo, funcionários e setor (LinkedIn ou à mão). LF: Receita, notícias, site e cargos-chave.
    Nas duas: quem decide mapeado (a área da vertical ou um executivo) e ponte por e-mail (alguém da empresa que
    troca e-mails com a equipe). Sem isso a equipe teria de buscar na mão."""
    if vertical == "saude":
        falta = faltando_saude(e)
    elif vertical == "linhas_financeiras":
        falta = faltando_lf(e)
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
    return (p.score_relacionamento or 0) >= 20 or (relacao is not None and p.id in relacao)


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
            "influencia": influencia(p)["score"]}


def quem_decide(db: Session, e: Empresa | None, vertical: str | None, nomes: dict[str, str],
                pontes: dict[int, str] | None = None, relacao: set[int] | None = None) -> dict | None:
    """Pessoas da área que costuma decidir a vertical e, se ninguém dela tem relação, a ponte:
    o contato da empresa com relação mais forte com alguém da equipe."""
    if e is None or vertical not in AREAS_VERTICAL:
        return None
    areas = AREAS_VERTICAL[vertical]
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


def produtos_da_empresa(e: Empresa, vigentes: list[Negocio]) -> dict:
    """Linhas Financeiras por produto (E&O, D&O, Cyber, IMI): sugere os que a empresa não tem nem negocia, com
    as etiquetas do que está em negociação e do que já foi ofertado e perdido. Garantia e Fiança não entram."""
    lf = [n for n in e.negocios if n.vertical == "linhas_financeiras"]
    chave = lambda n: produto_lf(n.produto or n.titulo)  # noqa: E731
    tem = {chave(n) for n in vigentes if n.vertical == "linhas_financeiras"} - {None}
    abertos = [n for n in lf if n.status == "aberto"]
    negociando = {chave(n) for n in abertos} - {None}
    perdidos: dict[str, Negocio] = {}
    for n in lf:
        k = chave(n)
        if n.status == "perdido" and k and k not in tem | negociando:
            if k not in perdidos or (n.perdido_em or date.min) > (perdidos[k].perdido_em or date.min):
                perdidos[k] = n
    excluir = tem | negociando
    todos = produtos_lf(e, excluir)
    sugeridos = [x for x in todos if x["sugerido"]] or todos[:1]
    etiquetas = [f"negociando {FITS[k][0]} agora" for k in sorted(negociando)]
    etiquetas += [f"{FITS[k][0]} ofertado em {n.perdido_em:%m/%Y}, perdido" if n.perdido_em
                  else f"{FITS[k][0]} já ofertado, perdido" for k, n in sorted(perdidos.items())]
    ja_lf = any(n.vertical == "linhas_financeiras" for n in vigentes) or bool(abertos)
    return {"tipo": "mais_lf" if ja_lf else "cross", "excluir": excluir, "etiquetas": etiquetas,
            "produtos": [{"produto": x["produto"], "valor": x["valor"], "texto": x["texto"]} for x in sugeridos]}


def oportunidades(db: Session, settings: Settings, hoje: date | None = None) -> list[dict]:
    """Verticais que a empresa ainda não tem nem está negociando, para clientes (seguro vigente) e
    leads com algum card aberto no Pipedrive (negócio aberto com vertical, em qualquer funil).
    Quem não tem nada aberto fica de fora (vai para uma tela própria, ainda a desenhar).
    Ordenadas pelo Potencial na vertical da oportunidade."""
    hoje = hoje or date.today()
    funis = settings.pipelines()
    nomes = {u.email: u.nome for u in db.scalars(select(Usuario))}
    # Só empresas com algum card aberto no Pipedrive. Canais Parceria (sem vertical) são parceiros,
    # não leads de seguro: não contam
    ids = select(Negocio.empresa_id).where(
        Negocio.empresa_id.is_not(None), Negocio.vertical.is_not(None),
        Negocio.status == "aberto", Negocio.fonte == "pipedrive").distinct()
    # Tudo de uma vez (antes eram milhares de consultas, uma por empresa, vertical e pessoa)
    empresas = db.scalars(select(Empresa).where(Empresa.id.in_(ids)).options(
        selectinload(Empresa.negocios), selectinload(Empresa.pessoas), selectinload(Empresa.noticias))).all()
    relacoes_de: dict[int, set[str]] = defaultdict(set)
    for empresa_id, email in db.execute(select(Interacao.empresa_id, Interacao.usuario_email)
                                        .where(Interacao.empresa_id.in_(ids))):
        relacoes_de[empresa_id].add(email)
    pontes = _pontes(db, ids)
    relacao = com_relacao(db, ids)
    proxima_de: dict[int, Atividade] = {}
    for a in db.scalars(select(Atividade).where(Atividade.empresa_id.in_(ids), Atividade.negocio_id.is_(None),
                                                Atividade.concluida.is_(False)).order_by(Atividade.vencimento, Atividade.id)):
        proxima_de.setdefault(a.empresa_id, a)
    saida = []
    for e in empresas:
        if interna(e, settings):
            continue
        abertos = [n for n in e.negocios if n.status == "aberto" and n.fonte == "pipedrive" and n.vertical]
        if not abertos:
            continue
        vigentes = seguros_vigentes(e)
        tem = {v.vertical for v in vigentes}
        negociando = {n.vertical for n in e.negocios if n.status == "aberto" and n.vertical}
        # Quem da equipe troca e-mails com alguém da empresa (filtro "Relação de")
        relacoes = sorted(relacoes_de[e.id])
        for v in VERTICAIS:
            lf = produtos_da_empresa(e, vigentes) if v == "linhas_financeiras" else None
            if lf is not None:
                if not lf["produtos"]:
                    continue  # já tem ou negocia todos os produtos de LF
            elif v in tem or v in negociando:
                continue
            decide = quem_decide(db, e, v, nomes, pontes, relacao)
            alvo = db.get(Pessoa, decide["pessoas"][0]["id"]) if decide and decide["pessoas"] else None
            contato = alvo or melhor_contato(e)
            pot = potencial(v, e, contato, excluir=lf["excluir"] if lf else frozenset())
            prox = proxima_de.get(e.id)
            contatos = [c for c in e.pessoas if c.pipedrive_person_id]
            func, func_origem = _funcionarios(None, e)
            saida.append({
                "id": f"{e.id}-{v}", "vertical": v, "verticalNome": VERTICAL_LABEL[v],
                "score": pot["score"], "criterios": pot["criterios"],
                "influencia": influencia(contato)["score"], "cliente": bool(vigentes),
                "empresa": {"id": e.id, "nome": e.razao_social, "funcionarios": func, "funcionariosOrigem": func_origem,
                            "noPipedrive": bool(e.pipedrive_org_id), "local": local(e), "cidade": e.cidade, "uf": e.uf,
                            "cidadeFonte": e.cidade_fonte},
                "vigentes": [_vig_json(x) for x in vigentes], "saude": estado_saude(e, vigentes),
                "noticias": [{"titulo": x.titulo, "fonte": x.fonte, "url": x.url, "portal": portal(x.site),
                              "data": x.publicada_em.date().isoformat() if x.publicada_em else None} for x in e.noticias[:2]],
                "relacoes": relacoes,
                "negociando": [{"vertical": x.vertical, "produto": x.produto or x.titulo, "etapa": x.etapa,
                                "funil": funis.get(x.pipeline_id, {}).get("nome")} for x in abertos],
                "quemDecide": decide, "motivos": _extras(e, None, v, vigentes) + motivos_potencial(pot),
                "contatos": [{"id": c.id, "nome": c.nome, "cargo": c.cargo, "area": area_pessoa(c)} for c in contatos],
                "proximaAtividade": {"assunto": prox.assunto, "vencimento": prox.vencimento.isoformat(),
                                     "responsavel": prox.responsavel.email} if prox else None,
                "faltando": (falta := faltando(v, e, decide, any(tem_relacao(p, relacao) for p in e.pessoas))),
                "analisada": not falta,
                "praca": praca(e) if v == "saude" else None,
                # LF: "cross" (a empresa ainda não tem nem negocia LF) ou "mais_lf" (mais produtos para quem já tem);
                # produtos sugeridos e etiquetas ("negociando D&O agora", "Cyber ofertado em 03/2026, perdido")
                "tipo": lf["tipo"] if lf else "cross",
                "produtos": lf["produtos"] if lf else [],
                "etiquetas": lf["etiquetas"] if lf else [],
            })
    return sorted(saida, key=lambda x: (x["score"], x["influencia"]), reverse=True)


def semana(ref: date) -> tuple[date, date]:
    """Segunda a sexta da semana de `ref` (a reunião é na sexta)."""
    seg = ref - timedelta(days=ref.weekday())
    return seg, seg + timedelta(days=4)


def todos(db: Session, ref: date | None = None) -> dict:
    ref = ref or date.today()
    seg, sex = semana(ref)
    pend = db.scalars(select(Atividade).where(Atividade.concluida.is_(False), Atividade.vencimento <= sex)
                      .order_by(Atividade.vencimento)).all()
    feitas = db.scalars(select(Atividade).where(Atividade.concluida.is_(True), Atividade.vencimento >= seg,
                                                Atividade.vencimento <= sex)).all()

    return {"inicio": seg.isoformat(), "fim": sex.isoformat(),
            "itens": [item_atividade(a, ref) for a in [*pend, *feitas]]}


def item_atividade(a: Atividade, ref: date) -> dict:
    return {"id": a.id, "assunto": a.assunto, "tipo": a.tipo, "vencimento": a.vencimento.isoformat(),
            "atrasada": not a.concluida and a.vencimento < ref, "concluida": a.concluida,
            "responsavel": a.responsavel.email, "responsavelNome": a.responsavel.nome,
            "empresa": a.empresa.razao_social if a.empresa else None, "empresaId": a.empresa_id,
            "pessoa": a.pessoa.nome if a.pessoa else None, "negocio": a.negocio.titulo if a.negocio else None,
            "negocioId": a.negocio_id}
