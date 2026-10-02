"""Tabela de trabalho: negócios ABERTOS dos funis marcados com `tabela: true`.

Cada linha traz o que a reunião de sexta precisa para decidir o próximo passo:
o cliente/lead e o contato, os seguros vigentes que ele já tem conosco (por
produto), a temperatura dos e-mails, notícias, quem tem relação e a próxima
atividade.

score = 100 × (relacionamento + vínculo + momento + porte), com pesos por vertical:
                  relacionamento  vínculo  momento  porte
  Saúde                35%          25%      15%     25%
  demais               40%          30%      20%     10%

  relacionamento  score de e-mail do contato (ou da empresa), ajustado pela temperatura
  vínculo         já é cliente em outras verticais (cross sell) e/ou na mesma
  momento         renovação próxima de algum seguro vigente
  porte           nº de funcionários (LinkedIn > Pipedrive) ou de vidas do negócio de Saúde,
                  em escala log: 10 -> 0,33 · 100 -> 0,67 · 1.000+ -> 1
"""

from collections import Counter
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import AREAS_VERTICAL, VERTICAIS, VERTICAL_LABEL, Settings
from crosssell.connectors.linkedin import mudou_de_empresa
from crosssell.models import Atividade, Empresa, Interacao, Negocio, Pessoa, Usuario
from crosssell.normalize import AREA_LABEL, classificar_area

PESOS = {"saude": (0.35, 0.25, 0.15, 0.25), None: (0.40, 0.30, 0.20, 0.10)}  # rel, vínculo, momento, porte
PORTE_DESCONHECIDO = 0.3
FONTE_FUNC = {"linkedin": "no LinkedIn", "pipedrive": "no Pipedrive", "planilha": "na planilha"}
AJUSTE_TEMPERATURA = {"muita": 0.15, "media": 0.0, "pouca": -0.15}
DECISORES = {"socio", "c_level", "diretor"}
NIVEL = {"socio": 0, "c_level": 0, "diretor": 1, "gerente": 2}


def area_pessoa(p: Pessoa) -> str | None:
    return classificar_area(p.cargo) or classificar_area(p.linkedin_headline)


def _pessoa_json(db: Session, p: Pessoa, nomes: dict[str, str]) -> dict:
    proximo = _ponte(db, p.id, None)
    area = area_pessoa(p)
    return {"id": p.id, "nome": p.nome, "cargo": p.cargo or p.linkedin_headline, "area": area,
            "areaNome": AREA_LABEL.get(area or ""), "linkedin": p.linkedin_url, "fonte": p.fonte,
            "noPipedrive": bool(p.pipedrive_person_id), "relacao": round(p.score_relacionamento or 0),
            "temperatura": p.temperatura, "quemFala": nomes.get(proximo, proximo) if proximo else None}


def quem_decide(db: Session, e: Empresa | None, vertical: str | None, nomes: dict[str, str]) -> dict | None:
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
        ponte = _pessoa_json(db, rel[0], nomes) if rel else None
    return {"areas": [AREA_LABEL[a] for a in areas], "pessoas": [_pessoa_json(db, p, nomes) for p in pessoas],
            "daArea": bool(da_area), "ponte": ponte}


def _motivo_decide(dec: dict | None, vertical: str) -> list[str]:
    if dec is None:
        return []
    rotulo = VERTICAL_LABEL[vertical]
    if not dec["daArea"]:
        texto = f"ainda não sabemos quem decide {rotulo} ({' / '.join(dec['areas'])})"
        if dec["ponte"]:
            texto += f" — {dec['ponte']['nome']} pode indicar"
        return [texto]
    alvo = dec["pessoas"][0]
    if alvo["relacao"] >= 20:
        return []
    texto = f"quem decide {rotulo}: {alvo['nome']} ({alvo['cargo']}) — sem relação ainda"
    if dec["ponte"]:
        pt = dec["ponte"]
        texto += f"; ponte: {pt['nome']}" + (f" (fala com {pt['quemFala']})" if pt["quemFala"] else "")
    return [texto]


def seguros_vigentes(e: Empresa | None) -> list[Negocio]:
    if e is None:
        return []
    return sorted((n for n in e.negocios if n.vigente and n.vertical),
                  key=lambda n: (n.vertical, n.produto or n.titulo or ""))


def _momento(vigentes: list[Negocio], hoje: date) -> tuple[float, list[str]]:
    melhor, motivos = 0.4, []
    for n in vigentes:
        if not n.fim_vigencia:
            continue
        dias = (n.fim_vigencia - hoje).days
        nivel = 1.0 if 30 <= dias <= 120 else (0.7 if 0 <= dias < 30 else None)
        if nivel is None:
            continue
        texto = f"renovação de {n.produto or VERTICAL_LABEL[n.vertical]} em {dias} dias — gancho para a conversa"
        if nivel > melhor:
            melhor, motivos = nivel, [texto]
        elif nivel == melhor:
            motivos.append(texto)
    return melhor, motivos


def _porte(n: Negocio, e: Empresa | None) -> tuple[float, int | None, str, list[str]]:
    """Valor do porte (0–1), número usado, de onde veio e o motivo para o "Por quê"."""
    import math

    if n.vertical == "saude" and n.vidas:
        numero, origem = n.vidas, "vidas no negócio"
    elif e is not None and e.funcionarios:
        numero, origem = e.funcionarios, f"funcionários {FONTE_FUNC.get(e.funcionarios_fonte or '', '')}".strip()
    else:
        return PORTE_DESCONHECIDO, None, "", []
    valor = max(0.0, min(1.0, math.log10(max(numero, 1)) / 3))
    texto = f"{numero:,}".replace(",", ".") + f" {origem}"
    if n.vertical == "saude":
        if numero >= 30:
            return valor, numero, origem, [f"{texto}: porte para plano coletivo"]
        if numero < 10:
            return valor, numero, origem, [f"{texto}: porte pequeno para saúde coletivo"]
        return valor, numero, origem, []
    return valor, numero, origem, ([f"{texto}: empresa de grande porte"] if numero >= 200 else [])


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


def _proxima_atividade(db: Session, negocio_id: int) -> Atividade | None:
    return db.scalar(select(Atividade).where(Atividade.negocio_id == negocio_id, Atividade.concluida.is_(False))
                     .order_by(Atividade.vencimento))


def linha(db: Session, n: Negocio, funis: dict, nomes: dict[str, str], hoje: date) -> dict:
    e, p = n.empresa, n.pessoa
    vigentes = seguros_vigentes(e)
    verticais_cli = {v.vertical for v in vigentes}
    outras = verticais_cli - {n.vertical}

    base = (p.score_relacionamento if p else (e.score_relacionamento if e else 0)) / 100
    temp = p.temperatura if p else None
    rel = max(0.0, min(1.0, base + AJUSTE_TEMPERATURA.get(temp or "", 0)))
    vinc = min(1.0, (0.7 if len(outras) == 1 else 1.0 if len(outras) > 1 else 0) + (0.3 if n.vertical in verticais_cli else 0))
    mom, mot_mom = _momento(vigentes, hoje)
    porte, func, func_origem, mot_porte = _porte(n, e)
    w_rel, w_vinc, w_mom, w_porte = PESOS.get(n.vertical if n.vertical == "saude" else None)

    motivos = []
    if outras:
        motivos.append("já é cliente de " + ", ".join(VERTICAL_LABEL[v] for v in sorted(outras)) + " — cross sell")
    if not vigentes:
        motivos.append("lead: nenhum seguro vigente conosco")
    ex = [x for x in (e.negocios if e else []) if x.vertical == n.vertical and x.ex_cliente and x.fim_vigencia]
    if ex and n.vertical not in verticais_cli:
        fim = max(x.fim_vigencia for x in ex)
        motivos.append(f"já teve {VERTICAL_LABEL[n.vertical]} conosco até {fim:%m/%Y} — reconquista")
    motivos += mot_mom + mot_porte
    if e is not None and e.score_componentes.get("acesso_decisor"):
        motivos.append("relação ativa com decisor")

    if p is not None and mudou_de_empresa(p):
        motivos.append(f"LinkedIn indica que {p.nome.split()[0]} hoje está em {p.linkedin_empresa_atual} — confirmar o contato")
    relevantes = set(AREAS_VERTICAL.get(n.vertical, ())) | {"executivo", None}  # áreas que importam para o negócio
    sem_relacao = [x for x in (e.pessoas if e else []) if x.fonte == "linkedin" and x.senioridade in DECISORES
                   and not x.score_relacionamento and area_pessoa(x) in relevantes]
    for x in sem_relacao[:2]:
        motivos.append(f"decisor no LinkedIn sem relação ainda: {x.nome} ({x.linkedin_headline or x.cargo})")

    decide = quem_decide(db, e, n.vertical, nomes)
    motivos += _motivo_decide(decide, n.vertical) if n.vertical else []

    ponte = _ponte(db, p.id if p else None, e.id if e else None)
    if ponte and n.responsavel_email and ponte != n.responsavel_email:
        motivos.append(f"{nomes.get(ponte, ponte)} tem relação com o contato — pode apresentar")

    contatos = [c for c in (e.pessoas if e else []) if c.pipedrive_person_id]
    if p is not None and p.pipedrive_person_id and p not in contatos:
        contatos.insert(0, p)
    prox = _proxima_atividade(db, n.id)
    saude_manual = next((x for x in (e.negocios if e else []) if x.fonte == "manual" and x.vertical == "saude"), None)
    funil = funis.get(n.pipeline_id, {})
    return {
        "id": n.id, "pipedriveId": n.id_externo, "titulo": n.titulo, "produto": n.produto, "etapa": n.etapa,
        "funil": funil.get("nome"), "vertical": n.vertical, "valor": n.valor,
        "score": round(100 * (w_rel * rel + w_vinc * vinc + w_mom * mom + w_porte * porte), 1),
        "comp": {"relacionamento": round(rel, 3), "vinculo": round(vinc, 3), "momento": mom, "porte": round(porte, 3)},
        "empresa": {"id": e.id, "nome": e.razao_social, "funcionarios": func, "funcionariosOrigem": func_origem}
        if e else None,
        "pessoa": {"id": p.id, "nome": p.nome, "cargo": p.cargo, "temperatura": temp,
                   "temperaturaMotivo": p.temperatura_motivo, "linkedin": p.linkedin_url,
                   "headline": p.linkedin_headline} if p else None,
        "contatos": [{"id": c.id, "nome": c.nome, "cargo": c.cargo, "area": area_pessoa(c)} for c in contatos],
        "quemDecide": decide,
        "vigentes": [{"vertical": v.vertical, "produto": v.produto or v.titulo, "fim": v.fim_vigencia.isoformat() if v.fim_vigencia else None,
                      "fonte": v.fonte} for v in vigentes],
        "saude": {"zeca": any(v.vertical == "saude" and v.fonte == "zeca" for v in vigentes),
                  "manual": bool(saude_manual and saude_manual.status == "ativo")},
        "noticias": [{"titulo": x.titulo, "fonte": x.fonte, "url": x.url,
                      "data": x.publicada_em.date().isoformat() if x.publicada_em else None} for x in (e.noticias[:3] if e else [])],
        "motivos": motivos, "ponte": ponte, "dono": n.responsavel_email,
        "proximaAtividade": {"assunto": prox.assunto, "vencimento": prox.vencimento.isoformat(),
                             "responsavel": prox.responsavel.email} if prox else None,
    }


def montar(db: Session, settings: Settings, hoje: date | None = None) -> list[dict]:
    hoje = hoje or date.today()
    funis = settings.pipelines()
    ids = [pid for pid, f in funis.items() if f["tabela"]]
    nomes = {u.email: u.nome for u in db.scalars(select(Usuario))}
    negocios = db.scalars(select(Negocio).where(Negocio.fonte == "pipedrive", Negocio.status == "aberto",
                                                Negocio.pipeline_id.in_(ids))).all()
    linhas = [linha(db, n, funis, nomes, hoje) for n in negocios]
    return sorted(linhas, key=lambda x: x["score"], reverse=True)


def oportunidades(db: Session, settings: Settings, hoje: date | None = None) -> list[dict]:
    """Clientes (seguro vigente em alguma vertical) sem seguro nem negócio aberto em outra vertical.

    score = 100 × (40% relação com a empresa + 25% porte + 20% vínculo + 15% sabemos quem decide)
    """
    import math

    hoje = hoje or date.today()
    nomes = {u.email: u.nome for u in db.scalars(select(Usuario))}
    ids = set(db.scalars(select(Negocio.empresa_id).where(Negocio.empresa_id.is_not(None))))
    saida = []
    for e in db.scalars(select(Empresa).where(Empresa.id.in_(ids))):
        vigentes = seguros_vigentes(e)
        if not vigentes:
            continue
        tem = {v.vertical for v in vigentes}
        abertos = {n.vertical for n in e.negocios if n.status == "aberto" and n.vertical}
        porte = math.log10(max(e.funcionarios, 1)) / 3 if e.funcionarios else PORTE_DESCONHECIDO
        porte = max(0.0, min(1.0, porte))
        for v in VERTICAIS:
            if v in tem or v in abertos:
                continue
            decide = quem_decide(db, e, v, nomes)
            motivos = ["já é cliente de " + ", ".join(VERTICAL_LABEL[x] for x in sorted(tem))]
            ex = [x for x in e.negocios if x.vertical == v and x.ex_cliente and x.fim_vigencia]
            if ex:
                motivos.append(f"já teve {VERTICAL_LABEL[v]} conosco até {max(x.fim_vigencia for x in ex):%m/%Y} — reconquista")
            if e.funcionarios:
                origem = FONTE_FUNC.get(e.funcionarios_fonte or "", "")
                motivos.append(f"{e.funcionarios:,}".replace(",", ".") + f" funcionários {origem}".rstrip())
            motivos += _motivo_decide(decide, v)
            rel = (e.score_relacionamento or 0) / 100
            vinc = min(1.0, len(tem) / 2)
            conhece = 1.0 if decide and decide["daArea"] else 0.0
            prox = db.scalar(select(Atividade).where(Atividade.empresa_id == e.id, Atividade.negocio_id.is_(None),
                                                     Atividade.concluida.is_(False)).order_by(Atividade.vencimento))
            contatos = [c for c in e.pessoas if c.pipedrive_person_id]
            saida.append({
                "id": f"{e.id}-{v}", "vertical": v, "verticalNome": VERTICAL_LABEL[v],
                "score": round(100 * (0.40 * rel + 0.25 * porte + 0.20 * vinc + 0.15 * conhece), 1),
                "empresa": {"id": e.id, "nome": e.razao_social, "funcionarios": e.funcionarios,
                            "noPipedrive": bool(e.pipedrive_org_id)},
                "vigentes": [{"vertical": x.vertical, "produto": x.produto or x.titulo,
                              "fim": x.fim_vigencia.isoformat() if x.fim_vigencia else None} for x in vigentes],
                "quemDecide": decide, "motivos": motivos,
                "contatos": [{"id": c.id, "nome": c.nome, "cargo": c.cargo, "area": area_pessoa(c)} for c in contatos],
                "proximaAtividade": {"assunto": prox.assunto, "vencimento": prox.vencimento.isoformat(),
                                     "responsavel": prox.responsavel.email} if prox else None,
            })
    return sorted(saida, key=lambda x: x["score"], reverse=True)


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
