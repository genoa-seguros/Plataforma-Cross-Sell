"""Motor de oportunidades de cross sell ("white space").

Para cada cliente (empresa com produto vigente em alguma vertical), cada vertical
corporativa em que ele ainda NÃO é cliente e NÃO tem negócio aberto vira uma
oportunidade. Linhas Pessoais é tratada no nível da pessoa: sócios e executivos
de clientes corporativos que ainda não têm apólice PF — e, no sentido inverso,
clientes PF que são sócios de empresas que ainda não são clientes.

score = 100 × (45% relacionamento + 30% aderência + 25% momento)
"""

from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.config import VERTICAL_LABEL
from crosssell.models import Empresa, Interacao, Negocio, Oportunidade, Pessoa
from crosssell.scoring.relacionamento import verticais_vigentes

CORPORATIVAS = ("linhas_financeiras", "saude", "ramos_elementares")
PESO_REL, PESO_FIT, PESO_MOM = 0.45, 0.30, 0.25
EXECUTIVOS = {"socio": 1.0, "c_level": 1.0, "diretor": 0.8}


def _divisao_cnae(empresa: Empresa) -> int | None:
    digitos = "".join(c for c in (empresa.cnae or "")[:2] if c.isdigit())
    return int(digitos) if len(digitos) == 2 else None


def aderencia(empresa: Empresa, alvo: str) -> tuple[float, list[str]]:
    motivos: list[str] = []
    fit = 0.5
    porte = (empresa.porte or "").upper()
    func = empresa.funcionarios or 0
    capital = empresa.capital_social or 0
    pequena = "MICRO" in porte or porte in ("ME", "01")
    div = _divisao_cnae(empresa)

    if alvo == "saude":
        if func >= 30:
            fit += 0.3
            motivos.append(f"{func} funcionários — porte para plano coletivo")
        elif func and func < 5:
            fit -= 0.2
        if pequena:
            fit -= 0.1
    elif alvo == "linhas_financeiras":
        if capital >= 1_000_000 or func >= 50:
            fit += 0.3
            motivos.append("porte compatível com D&O / garantia / crédito")
        if pequena:
            fit -= 0.2
    elif alvo == "ramos_elementares":
        fit += 0.1  # patrimonial é relevante para quase toda PJ
        if div is not None and (10 <= div <= 33 or 49 <= div <= 53):
            fit += 0.2
            motivos.append("atividade industrial/logística — riscos patrimoniais e de transporte")
    return max(0.0, min(1.0, fit)), motivos


def momento(negocios: list[Negocio], hoje: date) -> tuple[float, list[str]]:
    """Renovação próxima em qualquer vertical é o melhor gancho para abrir a conversa."""
    melhor, motivos = 0.4, []
    for n in negocios:
        if not n.vigente:
            continue
        fim = n.fim_vigencia
        if fim is None and n.fonte == "pipedrive" and n.inicio_vigencia:
            fim = n.inicio_vigencia + timedelta(days=365)  # seguros anuais
        if fim is None:
            continue
        dias = (fim - hoje).days
        nivel = 1.0 if 30 <= dias <= 120 else (0.7 if 0 <= dias < 30 else None)
        if nivel is None:
            continue
        texto = f"renovação de {VERTICAL_LABEL[n.vertical]} em {dias} dias"
        if nivel > melhor:
            melhor, motivos = nivel, [texto]
        elif nivel == melhor and texto not in motivos:
            motivos.append(texto)
    return melhor, motivos


def _ponte(db: Session, empresa_id: int | None, pessoa_id: int | None) -> str | None:
    """Usuário interno com mais interações com a empresa/pessoa — quem deve fazer a apresentação."""
    q = select(Interacao.usuario_email)
    q = q.where(Interacao.pessoa_id == pessoa_id) if pessoa_id else q.where(Interacao.empresa_id == empresa_id)
    cont = Counter(db.scalars(q))
    return cont.most_common(1)[0][0] if cont else None


def _score(rel: float, fit: float, mom: float) -> float:
    return round(100 * (PESO_REL * rel + PESO_FIT * fit + PESO_MOM * mom), 1)


def calcular(db: Session, hoje: date | None = None) -> dict:
    hoje = hoje or date.today()
    candidatas: dict[tuple, dict] = {}

    # Clientes PF (Linhas Pessoais) por nome, para cruzar com sócios do QSA.
    clientes_pf: dict[str, Pessoa] = {}
    for p in db.scalars(select(Pessoa).where(Pessoa.empresa_id.is_(None))):
        if "linhas_pessoais" in verticais_vigentes(p.negocios) and len(p.nome_normalizado.split()) >= 3:
            clientes_pf[p.nome_normalizado] = p

    for e in db.scalars(select(Empresa)).all():
        vigentes = verticais_vigentes(e.negocios)
        abertas = {n.vertical for n in e.negocios if n.status == "aberto"}
        socios_pf = [clientes_pf[p.nome_normalizado] for p in e.pessoas
                     if p.senioridade == "socio" and p.nome_normalizado in clientes_pf]
        if not vigentes and not socios_pf:
            continue
        rel = e.score_relacionamento / 100
        mom, mot_mom = momento(e.negocios, hoje)
        atuais = sorted(vigentes | ({"linhas_pessoais"} if socios_pf else set()))

        for alvo in CORPORATIVAS:
            if alvo in vigentes or alvo in abertas:
                continue
            fit, mot_fit = aderencia(e, alvo)
            motivos = [f"cliente de {', '.join(VERTICAL_LABEL[v] for v in atuais)}"]
            motivos += [f"sócio {s.nome} é cliente de Linhas Pessoais" for s in socios_pf]
            motivos += mot_fit + mot_mom
            if e.score_componentes.get("acesso_decisor"):
                motivos.append("relação ativa com decisor")
            candidatas[(e.id, None, alvo)] = {
                "score": _score(rel, fit, mom), "motivos": motivos, "atuais": atuais,
                "componentes": {"relacionamento": round(rel, 3), "aderencia": round(fit, 3), "momento": mom},
            }

        # Linhas Pessoais para executivos e sócios de clientes corporativos.
        if not vigentes:
            continue
        for p in e.pessoas:
            peso = EXECUTIVOS.get(p.senioridade or "")
            if not peso or "linhas_pessoais" in verticais_vigentes(p.negocios):
                continue
            if p.nome_normalizado in clientes_pf:
                continue
            rel_p = max(p.score_relacionamento, e.score_relacionamento * 0.5) / 100
            motivos = [f"{p.cargo or p.senioridade} de cliente ({', '.join(VERTICAL_LABEL[v] for v in atuais)})"]
            motivos += mot_mom
            if p.ponto_focal:
                motivos.append("é ponto focal — relação direta com a Innoa")
            candidatas[(e.id, p.id, "linhas_pessoais")] = {
                "score": _score(rel_p, peso, mom), "motivos": motivos, "atuais": atuais,
                "componentes": {"relacionamento": round(rel_p, 3), "aderencia": peso, "momento": mom},
            }

    existentes = {(o.empresa_id, o.pessoa_id, o.vertical_alvo): o for o in db.scalars(select(Oportunidade))}
    agora = datetime.utcnow()
    for chave, c in candidatas.items():
        o = existentes.pop(chave, None)
        if o is None:
            o = Oportunidade(empresa_id=chave[0], pessoa_id=chave[1], vertical_alvo=chave[2])
            db.add(o)
        o.score, o.motivos, o.componentes, o.verticais_atuais = c["score"], c["motivos"], c["componentes"], c["atuais"]
        o.ponte_email = _ponte(db, chave[0], chave[1])
        o.calculado_em = agora
    # Oportunidades que deixaram de existir (ex.: cliente fechou a vertical) e não foram trabalhadas.
    for o in existentes.values():
        if o.status == "nova":
            db.delete(o)
    db.commit()
    return {"oportunidades": len(candidatas)}
