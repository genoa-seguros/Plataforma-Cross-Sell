"""O que a própria plataforma preencheu, campo a campo (Diário da análise).

Enquanto um passo de análise da rotina roda (`pipeline.registrar` põe a fonte em `Session.info["origem"]`), cada
empresa, pessoa ou notícia nova e cada campo alterado vira uma `AlteracaoDado`, com o valor antes e depois. O que vem
do Pipedrive e o que a equipe faz à mão não passam por aqui.
"""

from datetime import date, datetime

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from crosssell.models import AlteracaoDado, Empresa, Noticia, Pessoa

# Passos da rotina que são análise da plataforma (os outros, como o Pipedrive, não entram no histórico)
ORIGENS = {"receita": "Receita", "noticias": "Notícias", "sites-ia": "Site lido pela IA", "linkedin": "LinkedIn",
           "linkedin-retorno": "LinkedIn", "linkedin-sites": "LinkedIn achado no site", "cnpj-sites": "CNPJ achado no site",
           "dominios": "Domínios de e-mail", "email": "E-mails da equipe",
           "assinaturas": "Assinatura dos e-mails"}
CAMPOS_EMPRESA = {"cnpj": "CNPJ", "razao_receita": "razão social (Receita)", "cidade": "cidade", "uf": "UF",
                  "funcionarios": "funcionários", "setor": "setor", "porte": "porte", "cnae": "CNAE",
                  "natureza_juridica": "natureza jurídica", "linkedin_url": "página do LinkedIn", "website": "site",
                  "dominio": "domínio de e-mail", "investida": "investida por fundo (VC)", "praca": "praça",
                  "praca_fatia": "funcionários na praça", "descricao": "descrição (LinkedIn)",
                  "site_ia": "leitura do site pela IA"}
CAMPOS_PESSOA = {"cargo": "cargo", "email": "e-mail", "linkedin_url": "LinkedIn", "linkedin_headline": "título no LinkedIn",
                 "linkedin_empresa_atual": "empresa atual (LinkedIn)", "temperatura": "temperatura",
                 "telefone": "telefone", "nome": "nome"}


def _texto(campo: str, v) -> str | None:
    if v is None or v == "":
        return None
    if campo == "site_ia":
        return "lida"
    if campo == "praca_fatia" and isinstance(v, (int, float)):
        return f"{round(100 * v)}%"
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, int) and campo == "funcionarios":
        return f"{v:,}".replace(",", ".")
    return str(v)[:300]


@event.listens_for(Session, "before_flush")
def _registrar_alteracoes(session: Session, _ctx, _instancias) -> None:
    origem = session.info.get("origem")
    if origem not in ORIGENS:
        return
    novas = []
    for obj in session.new:
        if isinstance(obj, Pessoa):
            novas.append(AlteracaoDado(fonte=origem, empresa=obj.empresa, empresa_id=obj.empresa_id, pessoa=obj,
                                       campo="pessoa nova", depois=obj.nome + (f" · {obj.cargo}" if obj.cargo else "")))
        elif isinstance(obj, Noticia):
            novas.append(AlteracaoDado(fonte=origem, empresa_id=obj.empresa_id, campo="notícia",
                                       depois=(obj.titulo or "")[:300]))
        elif isinstance(obj, Empresa):
            novas.append(AlteracaoDado(fonte=origem, empresa=obj, campo="empresa nova", depois=obj.razao_social))
    for obj in session.dirty:
        campos = CAMPOS_EMPRESA if isinstance(obj, Empresa) else CAMPOS_PESSOA if isinstance(obj, Pessoa) else None
        if not campos or not session.is_modified(obj, include_collections=False):
            continue
        estado = inspect(obj)
        for campo, rotulo in campos.items():
            h = estado.attrs[campo].history
            if not h.has_changes():
                continue
            antes = _texto(campo, h.deleted[0] if h.deleted else None)
            depois = _texto(campo, h.added[0] if h.added else None)
            if antes == depois and campo != "site_ia":
                continue
            if isinstance(obj, Empresa):
                novas.append(AlteracaoDado(fonte=origem, empresa_id=obj.id, campo=rotulo, antes=antes, depois=depois))
            else:
                novas.append(AlteracaoDado(fonte=origem, empresa_id=obj.empresa_id, pessoa_id=obj.id,
                                           campo=f"{rotulo} de {obj.nome}", antes=antes, depois=depois))
    session.add_all(novas)
