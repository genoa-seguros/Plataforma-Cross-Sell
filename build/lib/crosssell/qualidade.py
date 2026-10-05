"""Qualidade do cadastro do Pipedrive (revisão semanal, aba Qualidade).

1. Organizações duplicadas: mesmo CNPJ (certeza), mesmo nome sem sufixos societários ou
   mesmo domínio do site. A plataforma sugere qual manter (a que tem mais negócios ganhos,
   depois mais negócios, CNPJ e a mais antiga) e, com aprovação do master, mescla no
   Pipedrive (PUT /v1/organizations/{id}/merge): negócios, pessoas, atividades e notas
   passam para a que fica. Mesclar não tem volta, por isso nada é automático.
2. Razão social: compara o nome da organização com a razão social da Receita (pelo CNPJ)
   e sugere o nome completo. Atualizar também depende de aprovação.
"""

import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell.models import Empresa, QualidadeIgnorada
from crosssell.normalize import normalizar_nome_empresa, sem_acento

DOMINIOS_GENERICOS = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br", "uol.com.br",
                      "bol.com.br", "terra.com.br", "icloud.com", "live.com", "linkedin.com", "facebook.com",
                      "instagram.com", "wixsite.com", "google.com", "sites.google.com"}
MOTIVOS = {"cnpj": "mesmo CNPJ", "nome": "mesmo nome", "dominio": "mesmo site"}


def _ignoradas(db: Session) -> set[str]:
    return set(db.scalars(select(QualidadeIgnorada.chave)))


def chave_grupo(ids: list[int]) -> str:
    return "dup:" + ",".join(str(i) for i in sorted(ids))


def _resumo(e: Empresa) -> dict:
    negs = [n for n in e.negocios if n.fonte == "pipedrive"]
    return {"id": e.id, "orgId": e.pipedrive_org_id, "nome": e.razao_social, "cnpj": e.cnpj,
            "site": e.website or e.dominio, "negocios": len(negs), "ganhos": sum(n.status == "ganho" for n in negs),
            "pessoas": len(e.pessoas)}


def _ordem_manter(e: Empresa) -> tuple:
    negs = [n for n in e.negocios if n.fonte == "pipedrive"]
    return (-sum(n.status == "ganho" for n in negs), -len(negs), e.cnpj is None, e.pipedrive_org_id or 0)


def duplicadas(db: Session) -> list[dict]:
    """Grupos de organizações do Pipedrive que parecem ser a mesma empresa."""
    empresas = db.scalars(select(Empresa).where(Empresa.pipedrive_org_id.is_not(None))).all()
    pai = {e.id: e.id for e in empresas}
    motivo: dict[int, set] = defaultdict(set)

    def raiz(i):
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    for tipo, chave in (("cnpj", lambda e: e.cnpj),
                        ("nome", lambda e: e.nome_normalizado if len(e.nome_normalizado or "") >= 4 else None),
                        ("dominio", lambda e: e.dominio if e.dominio and e.dominio not in DOMINIOS_GENERICOS else None)):
        por: dict[str, list[Empresa]] = defaultdict(list)
        for e in empresas:
            k = chave(e)
            if k:
                por[k].append(e)
        for grupo in por.values():
            if len(grupo) < 2:
                continue
            for e in grupo[1:]:
                pai[raiz(e.id)] = raiz(grupo[0].id)
            for e in grupo:
                motivo[e.id].add(tipo)

    grupos: dict[int, list[Empresa]] = defaultdict(list)
    for e in empresas:
        grupos[raiz(e.id)].append(e)
    ignoradas = _ignoradas(db)
    saida = []
    for membros in grupos.values():
        if len(membros) < 2:
            continue
        ids = [e.id for e in membros]
        if chave_grupo(ids) in ignoradas:
            continue
        membros.sort(key=_ordem_manter)
        tipos = set().union(*(motivo[e.id] for e in membros))
        # CNPJs diferentes com o mesmo nome: matriz e filial ou homônimas; fica, mas avisado
        cnpjs = {e.cnpj for e in membros if e.cnpj}
        saida.append({"chave": chave_grupo(ids), "motivos": [MOTIVOS[t] for t in ("cnpj", "nome", "dominio") if t in tipos],
                      "certeza": "alta" if "cnpj" in tipos or ({"nome", "dominio"} <= tipos) else "média",
                      "cnpjsDiferentes": len(cnpjs) > 1,
                      "manter": _resumo(membros[0]), "mesclar": [_resumo(e) for e in membros[1:]]})
    ordem = {"alta": 0, "média": 1}
    return sorted(saida, key=lambda g: (ordem[g["certeza"]], g["cnpjsDiferentes"], -g["manter"]["negocios"]))


def mesclar(db: Session, client, manter_id: int, mesclar_ids: list[int]) -> dict:
    """Mescla no Pipedrive e na plataforma: tudo das organizações `mesclar_ids` passa para `manter_id`."""
    alvo = db.get(Empresa, manter_id)
    if alvo is None or not alvo.pipedrive_org_id:
        raise ValueError("Organização a manter não encontrada no Pipedrive.")
    feitas = []
    for i in mesclar_ids:
        dup = db.get(Empresa, i)
        if dup is None or dup.id == alvo.id or not dup.pipedrive_org_id:
            continue
        client.mesclar_organizacao(dup.pipedrive_org_id, alvo.pipedrive_org_id)
        for n in list(dup.negocios):
            n.empresa = alvo
        for p in list(dup.pessoas):
            p.empresa = alvo
        for campo in ("cnpj", "razao_receita", "website", "dominio", "linkedin_url", "setor", "cnae", "funcionarios",
                      "cidade", "uf", "natureza_juridica", "descricao"):
            if getattr(alvo, campo) in (None, "") and getattr(dup, campo) not in (None, ""):
                setattr(alvo, campo, getattr(dup, campo))
        feitas.append(dup.pipedrive_org_id)
        db.flush()
        db.delete(dup)
    db.commit()
    return {"mescladas": len(feitas), "manter": alvo.pipedrive_org_id}


# --- Razão social ----------------------------------------------------------------

_MINUSCULAS = {"de", "da", "do", "das", "dos", "e", "em", "para", "com"}
_SIGLAS = {"ltda": "Ltda", "s/a": "S/A", "s.a.": "S.A.", "sa": "S.A.", "me": "ME", "epp": "EPP", "eireli": "EIRELI",
           "cia": "Cia", "cia.": "Cia."}


def formatar_razao(razao: str, atual: str = "") -> str:
    """"SALVY TECNOLOGIA LTDA" -> "Salvy Tecnologia Ltda" (siglas societárias e conectivos ajustados).
    A Receita não usa acentos: palavras iguais às do nome atual recuperam a grafia dele."""
    grafia = {sem_acento(w).lower(): w for w in re.findall(r"[\wÀ-ÿ]+", atual or "")}
    saida = []
    for i, p in enumerate(razao.split()):
        b = p.lower()
        if b in _SIGLAS:
            saida.append(_SIGLAS[b])
        elif i and b in _MINUSCULAS:
            saida.append(b)
        else:
            w = grafia.get(b, p)
            saida.append(w[:1].upper() + w[1:].lower())
    return " ".join(saida)


def _comparavel(nome: str) -> str:
    return re.sub(r"[^a-z0-9]", "", sem_acento(nome or "").lower())


def razao_social(db: Session) -> dict:
    """Organizações cujo nome no Pipedrive não é a razão social completa da Receita."""
    ignoradas = _ignoradas(db)
    sugestoes, sem_cnpj, aguardando = [], 0, 0
    for e in db.scalars(select(Empresa).where(Empresa.pipedrive_org_id.is_not(None))):
        if not e.cnpj:
            sem_cnpj += 1
            continue
        if not e.razao_receita:
            aguardando += 1
            continue
        nova = formatar_razao(e.razao_receita, e.razao_social)
        if _comparavel(nova) == _comparavel(e.razao_social) or f"razao:{e.id}" in ignoradas:
            continue
        sugestoes.append({"chave": f"razao:{e.id}", "id": e.id, "orgId": e.pipedrive_org_id, "atual": e.razao_social,
                          "sugerida": nova, "cnpj": e.cnpj,
                          "negocios": sum(1 for n in e.negocios if n.fonte == "pipedrive")})
    sugestoes.sort(key=lambda s: -s["negocios"])
    return {"sugestoes": sugestoes, "semCnpj": sem_cnpj, "aguardandoReceita": aguardando}


def aplicar_razao(db: Session, client, empresa_id: int, nome: str) -> None:
    e = db.get(Empresa, empresa_id)
    if e is None or not e.pipedrive_org_id:
        raise ValueError("Organização não encontrada no Pipedrive.")
    nome = nome.strip()
    if not nome:
        raise ValueError("Informe o nome.")
    client.atualizar_organizacao(e.pipedrive_org_id, {"name": nome})
    e.razao_social = nome
    e.nome_normalizado = normalizar_nome_empresa(nome)
    db.commit()


def ignorar(db: Session, chave: str) -> None:
    if not db.scalar(select(QualidadeIgnorada).where(QualidadeIgnorada.chave == chave)):
        db.add(QualidadeIgnorada(chave=chave, em=datetime.utcnow()))
        db.commit()


def resumo(db: Session) -> dict:
    d, r = duplicadas(db), razao_social(db)
    return {"duplicadas": len(d), "duplicadasCnpj": sum("mesmo CNPJ" in g["motivos"] for g in d),
            "razaoSocial": len(r["sugestoes"]), "semCnpj": r["semCnpj"]}

