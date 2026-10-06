"""Qualidade do cadastro do Pipedrive (revisão semanal, aba Qualidade).

1. Organizações duplicadas: mesmo CNPJ (certeza), mesmo nome sem sufixos societários ou
   mesmo domínio do site. A plataforma sugere qual manter (a que tem mais negócios ganhos,
   depois mais negócios, CNPJ e a mais antiga) e, com aprovação do master, mescla no
   Pipedrive (PUT /v1/organizations/{id}/merge): negócios, pessoas, atividades e notas
   passam para a que fica. Mesclar não tem volta, por isso nada é automático.
2. Razão social: compara o nome da organização com a razão social da Receita (pelo CNPJ)
   e sugere o nome completo. Atualizar também depende de aprovação.

Desempenho: as contagens de negócios e pessoas vêm de duas consultas agrupadas e as empresas
são carregadas só com os campos usados aqui (antes era uma consulta por organização).
"""

import re
from collections import defaultdict
from datetime import datetime

import httpx
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, load_only

from crosssell.models import Atividade, Empresa, Interacao, Negocio, Noticia, Pessoa, QualidadeIgnorada
from crosssell.normalize import normalizar_nome_empresa, sem_acento

DOMINIOS_GENERICOS = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br", "uol.com.br",
                      "bol.com.br", "terra.com.br", "icloud.com", "live.com", "linkedin.com", "facebook.com",
                      "instagram.com", "wixsite.com", "google.com", "sites.google.com"}
MOTIVOS = {"cnpj": "mesmo CNPJ", "nome": "mesmo nome", "dominio": "mesmo site"}
POR_PAGINA = 20


def _ignoradas(db: Session) -> set[str]:
    return set(db.scalars(select(QualidadeIgnorada.chave)))


def chave_grupo(ids: list[int]) -> str:
    return "dup:" + ",".join(str(i) for i in sorted(ids))


def _contagens(db: Session) -> dict[int, tuple[int, int, int]]:
    """empresa_id -> (negócios do Pipedrive, ganhos, pessoas), em duas consultas."""
    negs = {i: (n, g or 0) for i, n, g in db.execute(
        select(Negocio.empresa_id, func.count(), func.sum(case((Negocio.status == "ganho", 1), else_=0)))
        .where(Negocio.fonte == "pipedrive", Negocio.empresa_id.is_not(None)).group_by(Negocio.empresa_id))}
    pessoas = dict(db.execute(select(Pessoa.empresa_id, func.count()).where(Pessoa.empresa_id.is_not(None))
                              .group_by(Pessoa.empresa_id)).all())
    return {i: (*negs.get(i, (0, 0)), pessoas.get(i, 0)) for i in negs.keys() | pessoas.keys()}


def _empresas(db: Session) -> list[Empresa]:
    """Organizações do Pipedrive, só com os campos que a revisão usa."""
    return db.scalars(select(Empresa).options(load_only(
        Empresa.id, Empresa.pipedrive_org_id, Empresa.razao_social, Empresa.nome_normalizado, Empresa.cnpj,
        Empresa.website, Empresa.dominio, Empresa.razao_receita)).where(Empresa.pipedrive_org_id.is_not(None))).all()


def _resumo(e: Empresa, cont: dict) -> dict:
    negocios, ganhos, pessoas = cont.get(e.id, (0, 0, 0))
    return {"id": e.id, "orgId": e.pipedrive_org_id, "nome": e.razao_social, "cnpj": e.cnpj,
            "site": e.website or e.dominio, "negocios": negocios, "ganhos": ganhos, "pessoas": pessoas}


def _ordem_manter(e: Empresa, cont: dict) -> tuple:
    negocios, ganhos, _ = cont.get(e.id, (0, 0, 0))
    return (-ganhos, -negocios, e.cnpj is None, e.pipedrive_org_id or 0)


def pagina(itens: list, n: int | None, por_pagina: int = POR_PAGINA) -> dict:
    """Recorte de uma lista: {itens, total, pagina, paginas}. Página fora do intervalo vai para a mais próxima."""
    paginas = max(1, -(-len(itens) // por_pagina))
    n = min(max(1, n or 1), paginas)
    return {"itens": itens[(n - 1) * por_pagina:n * por_pagina], "total": len(itens), "pagina": n, "paginas": paginas}


def duplicadas(db: Session) -> list[dict]:
    """Grupos de organizações do Pipedrive que parecem ser a mesma empresa."""
    empresas = _empresas(db)
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
    cont = _contagens(db) if any(len(m) > 1 for m in grupos.values()) else {}
    saida = []
    for membros in grupos.values():
        if len(membros) < 2:
            continue
        ids = [e.id for e in membros]
        if chave_grupo(ids) in ignoradas:
            continue
        membros.sort(key=lambda e: _ordem_manter(e, cont))
        tipos = set().union(*(motivo[e.id] for e in membros))
        # CNPJs diferentes com o mesmo nome: matriz e filial ou homônimas; fica, mas avisado
        cnpjs = {e.cnpj for e in membros if e.cnpj}
        saida.append({"chave": chave_grupo(ids), "motivos": [MOTIVOS[t] for t in ("cnpj", "nome", "dominio") if t in tipos],
                      "certeza": "alta" if "cnpj" in tipos or ({"nome", "dominio"} <= tipos) else "média",
                      "cnpjsDiferentes": len(cnpjs) > 1,
                      "manter": _resumo(membros[0], cont), "mesclar": [_resumo(e, cont) for e in membros[1:]]})
    ordem = {"alta": 0, "média": 1}
    return sorted(saida, key=lambda g: (ordem[g["certeza"]], g["cnpjsDiferentes"], -g["manter"]["negocios"]))


def _mesclar_no_pipedrive(client, org_id: int, manter_id: int) -> None:
    """Mescla no Pipedrive. Se ele responde que a organização não existe mais e a que fica existe, ela já foi
    mesclada (ex.: numa tentativa anterior que parou no meio): segue, para a plataforma terminar a mesclagem."""
    try:
        client.mesclar_organizacao(org_id, manter_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (400, 404, 410):
            raise
        existem = client.organizacoes([org_id, manter_id])
        if org_id in existem or manter_id not in existem:
            raise


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
        _mesclar_no_pipedrive(client, dup.pipedrive_org_id, alvo.pipedrive_org_id)
        for n in list(dup.negocios):
            n.empresa = alvo
        for p in list(dup.pessoas):
            p.empresa = alvo
        # Tudo que aponta para a organização que deixa de existir passa para a que fica
        # (sem isso o banco recusa a exclusão, depois de o Pipedrive já ter mesclado)
        for modelo in (Atividade, Interacao):
            db.execute(update(modelo).where(modelo.empresa_id == dup.id).values(empresa_id=alvo.id))
        ja_tem = set(db.scalars(select(Noticia.url).where(Noticia.empresa_id == alvo.id)))
        for x in list(dup.noticias):
            if x.url in ja_tem:
                db.delete(x)
            else:
                x.empresa_id = alvo.id
                ja_tem.add(x.url)
        db.flush()
        db.expire(dup, ["noticias"])
        for campo in ("cnpj", "razao_receita", "website", "dominio", "linkedin_url", "setor", "cnae", "funcionarios",
                      "cidade", "uf", "natureza_juridica", "descricao"):
            if getattr(alvo, campo) in (None, "") and getattr(dup, campo) not in (None, ""):
                setattr(alvo, campo, getattr(dup, campo))
        feitas.append(dup.pipedrive_org_id)
        db.flush()
        db.delete(dup)
        db.commit()  # já mesclada no Pipedrive: grava na hora, para uma falha na próxima não desfazer esta aqui
    return {"mescladas": len(feitas), "manter": alvo.pipedrive_org_id}


# --- Nome idêntico (mesclagem em lote, decidida pelo master) ----------------------
# Nome exatamente igual no Pipedrive (mesmas letras, acentos, maiúsculas e pontuação; só os espaços
# das pontas não contam) é a mesma organização, mesmo com CNPJ diferente ou em branco: o CNPJ não é
# obrigatório no Pipedrive e muitas vezes falta. Nomes parecidos continuam na lista de duplicadas.

def grupos_nome_identico(db: Session) -> list[tuple[str, list[Empresa]]]:
    """(nome, organizações) com o mesmo nome exato, em ordem alfabética; a primeira de cada grupo fica."""
    por: dict[str, list[Empresa]] = defaultdict(list)
    for e in _empresas(db):
        if (e.razao_social or "").strip():
            por[e.razao_social.strip()].append(e)
    grupos = [(nome, membros) for nome, membros in por.items() if len(membros) > 1]
    if grupos:
        cont = _contagens(db)
        for _, membros in grupos:
            membros.sort(key=lambda e: _ordem_manter(e, cont))
    return sorted(grupos, key=lambda g: g[0])


def mesclar_nomes_identicos(db: Session, client, apos: str = "", limite: int = 10) -> dict:
    """Mescla até `limite` grupos de nome idêntico, em ordem alfabética depois de `apos`.

    O nome gravado na plataforma pode estar desatualizado, então cada organização é conferida no
    Pipedrive antes: só entram as que lá têm exatamente o nome do grupo. A que mudou de nome passa a
    usar o nome do Pipedrive aqui (e sai do grupo); a que foi excluída lá fica de fora.
    Devolve `apos` para a próxima chamada e quantos grupos ainda faltam."""
    grupos = [g for g in grupos_nome_identico(db) if g[0] > apos]
    lote, resto = grupos[:limite], len(grupos) - min(limite, len(grupos))
    no_pipedrive = client.organizacoes([e.pipedrive_org_id for _, membros in lote for e in membros])
    cont = {"mescladas": 0, "grupos": 0, "conferir": 0, "falhas": 0}
    erros = []
    for nome, membros in lote:
        iguais = []
        for e in membros:
            org = no_pipedrive.get(e.pipedrive_org_id)
            atual = (org or {}).get("name", "").strip()
            if atual == nome:
                iguais.append(e)
            elif atual:  # renomeada no Pipedrive: a plataforma passa a usar o nome de lá
                e.razao_social, e.nome_normalizado = atual, normalizar_nome_empresa(atual)
        if len(iguais) < 2:
            cont["conferir"] += 1
            continue
        try:
            res = mesclar(db, client, iguais[0].id, [e.id for e in iguais[1:]])
        except Exception as exc:  # um grupo recusado não para os outros
            db.rollback()
            cont["falhas"] += 1
            erros.append(f"{nome}: {exc}")
            continue
        cont["mescladas"] += res["mescladas"]
        cont["grupos"] += 1
    db.commit()
    return {**cont, "apos": lote[-1][0] if lote else apos, "restantes": resto, "erros": erros}


# --- Cadastros suspeitos (exclusão decidida pelo master) ---------------------------
# Organizações que não são empresa de verdade: teste, "não tenho", pessoa física, nome sem letras.
# Comparação sem acento e em minúsculas. É uma lista para revisar: nada é excluído sem o master marcar.

SUSPEITAS = (
    ("teste", "Teste", (r"\btest(e|es|ando|ing)?\b", r"\bexemplo\b", r"\bfake\b", r"\basdf", r"\bx{3,}\b",
                        r"\bdummy\b", r"\bnao usar\b", r"\b(apagar|excluir|deletar)\b")),
    ("sem_empresa", "Sem empresa", (r"\bnao (tem|tenho|possui|sei|informad[oa]|definid[oa])\b", r"\bsem (empresa|nome)\b",
                                    r"^nenhuma?$", r"^n/?a$", r"\bdesconhecid[oa]\b")),
    ("pessoa_fisica", "Pessoa física", (r"\bpessoa fisica\b", r"\bpf\b", r"\bparticular\b", r"\bautonom[oa]\b")),
)
_PADROES = [(chave, rotulo, re.compile("|".join(regras))) for chave, rotulo, regras in SUSPEITAS]


def motivo_suspeita(nome: str | None) -> tuple[str, str] | None:
    """(chave, rótulo) do motivo pelo qual o nome não parece de uma empresa, ou None."""
    t = sem_acento(nome or "").lower().strip()
    if len(re.sub(r"[^a-z]", "", t)) < 2:  # vazio, só números ou só pontuação
        return ("invalido", "Nome inválido")
    for chave, rotulo, padrao in _PADROES:
        if padrao.search(t):
            return chave, rotulo
    return None


def chave_suspeita(empresa_id: int) -> str:
    return f"susp:{empresa_id}"


def suspeitas(db: Session) -> list[dict]:
    """Organizações do Pipedrive com nome suspeito, por motivo e nome (as marcadas "não é suspeita" ficam fora)."""
    ignoradas = _ignoradas(db)
    achadas = [(e, m) for e in _empresas(db) if chave_suspeita(e.id) not in ignoradas
               and (m := motivo_suspeita(e.razao_social))]
    cont = _contagens(db) if achadas else {}
    ordem = {c: i for i, (c, _, _) in enumerate((("invalido", 0, 0),) + SUSPEITAS)}
    saida = [{**_resumo(e, cont), "motivo": chave, "motivoNome": rotulo, "chave": chave_suspeita(e.id)}
             for e, (chave, rotulo) in achadas]
    return sorted(saida, key=lambda x: (ordem[x["motivo"]], (x["nome"] or "").lower()))


def _apagar_na_plataforma(db: Session, e: Empresa) -> None:
    """Tira a organização da plataforma como o Pipedrive faz: negócios, pessoas, atividades e e-mails ficam, sem ela."""
    for modelo in (Negocio, Pessoa, Atividade, Interacao):
        db.execute(update(modelo).where(modelo.empresa_id == e.id).values(empresa_id=None))
    for x in list(e.noticias):
        db.delete(x)
    db.flush()
    db.expire(e)
    db.delete(e)


def _motivo_pipedrive(exc: httpx.HTTPStatusError) -> str:
    """A mensagem de erro que o Pipedrive mandou (ou o código HTTP), sem a URL."""
    try:
        corpo = exc.response.json()
        return corpo.get("error") or corpo.get("message") or f"erro {exc.response.status_code}"
    except ValueError:
        return f"erro {exc.response.status_code}"


def excluir(db: Session, client, ids: list[int]) -> dict:
    """Exclui as organizações no Pipedrive e na plataforma. A que já foi excluída lá só sai daqui."""
    cont = {"excluidas": 0, "falhas": 0}
    erros = []
    for i in ids:
        e = db.get(Empresa, i)
        if e is None or not e.pipedrive_org_id:
            continue
        try:
            client.excluir_organizacao(e.pipedrive_org_id)
        except httpx.HTTPStatusError as exc:
            # O Pipedrive responde 400 ao excluir uma organização que já está excluída: confere antes de dar erro
            if exc.response.status_code not in (400, 404, 410) or e.pipedrive_org_id in client.organizacoes([e.pipedrive_org_id]):
                cont["falhas"] += 1
                erros.append(f"{e.razao_social}: {_motivo_pipedrive(exc)}")
                continue
        except httpx.HTTPError as exc:
            cont["falhas"] += 1
            erros.append(f"{e.razao_social}: {exc}")
            continue
        _apagar_na_plataforma(db, e)
        db.commit()  # já excluída no Pipedrive: grava na hora
        cont["excluidas"] += 1
    return {**cont, "erros": erros}


# --- Excluídas direto no Pipedrive ---------------------------------------------------
# Mais de 30% (e mais de 20) de uma vez parece falha da consulta, não exclusão de verdade
LIMITE_REMOCAO, MINIMO_SUSPEITO = 0.3, 20


def remover_excluidas(db: Session, client) -> dict:
    """Tira da plataforma as organizações que não existem mais no Pipedrive (excluídas ou mescladas lá).
    Negócios, pessoas, atividades e e-mails ficam, sem a organização, como no Pipedrive; a sincronização
    seguinte religa o que o Pipedrive mudou de organização. Roda de hora em hora, depois do Pipedrive."""
    existem = client.ids_organizacoes()
    locais = db.scalars(select(Empresa).where(Empresa.pipedrive_org_id.is_not(None))).all()
    sumiram = [e for e in locais if e.pipedrive_org_id not in existem]
    if sumiram and (not existem or len(sumiram) > max(MINIMO_SUSPEITO, LIMITE_REMOCAO * len(locais))):
        raise RuntimeError(f"{len(sumiram)} de {len(locais)} organizações sumiram do Pipedrive de uma vez; "
                           "nada foi removido (parece falha na consulta). Confira no Pipedrive.")
    for e in sumiram:
        _apagar_na_plataforma(db, e)
    db.commit()
    return {"removidas": len(sumiram)}


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


def razao_social(db: Session, n_pagina: int | None = None) -> dict:
    """Organizações cujo nome no Pipedrive não é a razão social completa da Receita.
    Com `n_pagina`, devolve só aquela página (POR_PAGINA sugestões) e o total."""
    ignoradas = _ignoradas(db)
    cont = _contagens(db)
    sugestoes, sem_cnpj, aguardando = [], 0, 0
    for e in _empresas(db):
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
                          "negocios": cont.get(e.id, (0, 0, 0))[0]})
    sugestoes.sort(key=lambda s: -s["negocios"])
    if n_pagina is None:
        return {"sugestoes": sugestoes, "semCnpj": sem_cnpj, "aguardandoReceita": aguardando}
    p = pagina(sugestoes, n_pagina)
    return {"sugestoes": p.pop("itens"), "sugestoesPagina": p, "semCnpj": sem_cnpj, "aguardandoReceita": aguardando}


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

