"""Tabela de trabalho: negócios ABERTOS dos funis marcados com `tabela: true`, e a aba
Oportunidades (verticais que a empresa ainda não tem nem está negociando).

Cada linha tem dois números (ver crosssell/potencial.py):
  Potencial   critérios de um bom negócio na vertical (ordena a tabela)
  Influência  quem é o contato: relacionamento por e-mail + hierarquia do cargo

"Por quê" traz só o que não aparece em outra coluna: critérios a favor e contra,
reconquista e contato que mudou de empresa.
"""

from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from crosssell.config import AREAS_VERTICAL, VERTICAIS, VERTICAL_LABEL, Settings
from crosssell.connectors.linkedin import mudou_de_empresa
from crosssell.models import Atividade, Empresa, Interacao, Negocio, Pessoa, Usuario
from crosssell.normalize import AREA_LABEL, classificar_area
from crosssell.potencial import influencia, melhor_contato, motivos as motivos_potencial, potencial

FONTE_FUNC = {"linkedin": "no LinkedIn", "pipedrive": "no Pipedrive", "planilha": "na planilha"}
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
                pontes: dict[int, str] | None = None) -> dict | None:
    """Pessoas da área que costuma decidir a vertical e, se ninguém dela tem relação, a ponte:
    o contato da empresa com relação mais forte com alguém da equipe."""
    if e is None or vertical not in AREAS_VERTICAL:
        return None
    areas = AREAS_VERTICAL[vertical]
    chave = lambda p: (-(p.score_relacionamento or 0), NIVEL.get(p.senioridade or "", 3), p.nome)  # noqa: E731
    da_area = sorted((p for p in e.pessoas if area_pessoa(p) in areas), key=chave)
    executivos = sorted((p for p in e.pessoas if area_pessoa(p) == "executivo"), key=chave)
    pessoas = da_area[:3] or executivos[:1]
    ponte = None
    if not any((p.score_relacionamento or 0) >= 20 for p in pessoas):
        rel = sorted((p for p in e.pessoas if (p.score_relacionamento or 0) >= 20 and p not in pessoas), key=chave)
        ponte = _pessoa_json(db, rel[0], nomes, pontes) if rel else None
    return {"areas": [AREA_LABEL[a] for a in areas], "pessoas": [_pessoa_json(db, p, nomes, pontes) for p in pessoas],
            "daArea": bool(da_area), "ponte": ponte}


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


def _funcionarios(n: Negocio | None, e: Empresa | None) -> tuple[int | None, str]:
    if n is not None and n.vertical == "saude" and n.vidas:
        return n.vidas, "vidas no negócio"
    if e is not None and e.funcionarios:
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


def oportunidades(db: Session, settings: Settings, hoje: date | None = None) -> list[dict]:
    """Verticais que a empresa ainda não tem nem está negociando, para clientes (seguro vigente)
    e para leads que já estão sendo trabalhados em outra vertical (negócio aberto nos funis da tabela).
    Ordenadas pelo Potencial na vertical da oportunidade."""
    hoje = hoje or date.today()
    funis = settings.pipelines()
    da_tabela = [pid for pid, f in funis.items() if f["tabela"]]
    nomes = {u.email: u.nome for u in db.scalars(select(Usuario))}
    # Só empresas que podem passar no filtro abaixo: algum negócio com vertical que esteja ganho/ativo
    # (só esses podem estar vigentes) ou aberto no Pipedrive num funil da tabela
    ids = select(Negocio.empresa_id).where(
        Negocio.empresa_id.is_not(None), Negocio.vertical.is_not(None),
        or_(Negocio.status.in_(("ganho", "ativo")),
            and_(Negocio.status == "aberto", Negocio.fonte == "pipedrive", Negocio.pipeline_id.in_(da_tabela)))).distinct()
    # Tudo de uma vez (antes eram milhares de consultas, uma por empresa, vertical e pessoa)
    empresas = db.scalars(select(Empresa).where(Empresa.id.in_(ids)).options(
        selectinload(Empresa.negocios), selectinload(Empresa.pessoas), selectinload(Empresa.noticias))).all()
    relacoes_de: dict[int, set[str]] = defaultdict(set)
    for empresa_id, email in db.execute(select(Interacao.empresa_id, Interacao.usuario_email)
                                        .where(Interacao.empresa_id.in_(ids))):
        relacoes_de[empresa_id].add(email)
    pontes = _pontes(db, ids)
    proxima_de: dict[int, Atividade] = {}
    for a in db.scalars(select(Atividade).where(Atividade.empresa_id.in_(ids), Atividade.negocio_id.is_(None),
                                                Atividade.concluida.is_(False)).order_by(Atividade.vencimento, Atividade.id)):
        proxima_de.setdefault(a.empresa_id, a)
    saida = []
    for e in empresas:
        if interna(e, settings):
            continue
        vigentes = seguros_vigentes(e)
        # Canais Parceria (sem vertical) são parceiros, não leads de seguro: não geram oportunidade
        abertos_tabela = [n for n in e.negocios if n.status == "aberto" and n.fonte == "pipedrive"
                          and n.pipeline_id in da_tabela and n.vertical]
        if not vigentes and not abertos_tabela:
            continue
        tem = {v.vertical for v in vigentes}
        negociando = {n.vertical for n in e.negocios if n.status == "aberto" and n.vertical}
        # Quem da equipe troca e-mails com alguém da empresa (filtro "Relação de")
        relacoes = sorted(relacoes_de[e.id])
        for v in VERTICAIS:
            if v in tem or v in negociando:
                continue
            decide = quem_decide(db, e, v, nomes, pontes)
            alvo = db.get(Pessoa, decide["pessoas"][0]["id"]) if decide and decide["pessoas"] else None
            contato = alvo or melhor_contato(e)
            pot = potencial(v, e, contato)
            prox = proxima_de.get(e.id)
            contatos = [c for c in e.pessoas if c.pipedrive_person_id]
            func, func_origem = _funcionarios(None, e)
            saida.append({
                "id": f"{e.id}-{v}", "vertical": v, "verticalNome": VERTICAL_LABEL[v],
                "score": pot["score"], "criterios": pot["criterios"],
                "influencia": influencia(contato)["score"], "cliente": bool(vigentes),
                "empresa": {"id": e.id, "nome": e.razao_social, "funcionarios": func, "funcionariosOrigem": func_origem,
                            "noPipedrive": bool(e.pipedrive_org_id)},
                "vigentes": [_vig_json(x) for x in vigentes], "saude": estado_saude(e, vigentes),
                "noticias": [{"titulo": x.titulo, "fonte": x.fonte, "url": x.url,
                              "data": x.publicada_em.date().isoformat() if x.publicada_em else None} for x in e.noticias[:2]],
                "relacoes": relacoes,
                "negociando": [{"vertical": x.vertical, "produto": x.produto or x.titulo, "etapa": x.etapa,
                                "funil": funis.get(x.pipeline_id, {}).get("nome")} for x in abertos_tabela],
                "quemDecide": decide, "motivos": _extras(e, None, v, vigentes) + motivos_potencial(pot),
                "contatos": [{"id": c.id, "nome": c.nome, "cargo": c.cargo, "area": area_pessoa(c)} for c in contatos],
                "proximaAtividade": {"assunto": prox.assunto, "vencimento": prox.vencimento.isoformat(),
                                     "responsavel": prox.responsavel.email} if prox else None,
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
