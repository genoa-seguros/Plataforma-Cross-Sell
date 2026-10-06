"""Gera uma prévia estática (um único HTML, sem servidor) da plataforma a partir do banco.

Uso: DATABASE_URL=sqlite:///demo.db python scripts/exportar_preview.py saida.html [--exemplo] [--como email]

A página é a mesma interface do app (crosssell/web/app.html) com os dados embutidos;
ações como criar atividade ficam só no navegador e não vão ao Pipedrive.
"""

import base64
import json
import re
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select

from crosssell import qualidade, tabela
from crosssell.config import VERTICAIS, VERTICAL_LABEL, get_settings
from crosssell.connectors import linkedin as lk
from crosssell.db import SessionLocal, init_db
from crosssell.models import Atividade, Usuario
from crosssell.web import app as webapp

APP = Path(__file__).resolve().parents[1] / "crosssell" / "web" / "app.html"


def _rotina_exemplo(r: dict, hoje: date) -> dict:
    """Aba Rotina na prévia: últimas execuções fictícias de uma rodada das 9h."""
    dia = hoje.isoformat()
    ok = lambda minuto, n: {"em": f"{dia}T09:{minuto:02d}", "registros": n, "erro": None}  # noqa: E731
    r["ultimos"] = {"pipedrive": ok(0, 128), "email": ok(2, 46), "noticias": ok(4, 12), "receita": ok(6, 9),
                    "linkedinSites": ok(7, 3), "linkedin": ok(8, 10), "qualidade": ok(10, 57)}
    r["interna"] = True
    r["email"].update(configurado=True, temperatura=True, caixas=4)
    r["linkedin"].update(configurado=True, ultimas24h=18)
    return r


def exportar(como: str | None) -> dict:
    init_db()
    db = SessionLocal()
    s = get_settings()
    linhas = tabela.montar(db, s)
    ids = {x["empresa"]["id"] for x in linhas if x["empresa"]} | {o["empresa"]["id"] for o in tabela.oportunidades(db, s)}
    empresas = {i: webapp.api_empresa(i, db=db, _u=None) for i in ids}
    for e in [*empresas.values(), *linhas]:
        for n in e["noticias"]:
            if not n["url"].startswith("http"):
                n["url"] = ""  # notícias fictícias não têm link
    usuarios = db.scalars(select(Usuario).order_by(Usuario.nome)).all()
    eu = next((u for u in usuarios if u.email == como), None) or next(u for u in usuarios if u.papel == "master")
    hoje = date.today()
    return {
        "eu": webapp._usuario_json(eu),
        "verticais": {v: VERTICAL_LABEL[v] for v in VERTICAIS},
        "linhas": linhas,
        "empresas": empresas,
        "equipe": [webapp._usuario_json(u) for u in usuarios],
        "atividades": [tabela.item_atividade(a, hoje) for a in db.scalars(select(Atividade))],
        "oportunidades": tabela.oportunidades(db, s),
        "qualidade": {"duplicadas": qualidade.duplicadas(db), **qualidade.razao_social(db)},
        "rotina": _rotina_exemplo(webapp.api_rotina(db=db, _u=None), hoje),
        "linkedin": {"configurado": True, "lote": s.linkedin_lote, **lk.situacao(db, s), "ultimas24h": 18,
                     "disparado": {"em": hoje.isoformat() + "T09:30", "registros": 10, "erro": None},
                     "recebido": {"em": hoje.isoformat() + "T09:52", "registros": 10, "erro": None}},
    }


def main():
    saida = Path(sys.argv[1])
    como = sys.argv[sys.argv.index("--como") + 1] if "--como" in sys.argv else None
    dados = exportar(como)
    html = APP.read_text(encoding="utf-8")
    # Sem servidor: logo e favicon vão embutidos na página
    for nome in ("logo-innoa-branco.png", "logo-innoa.png", "favicon.png"):
        dados_img = base64.b64encode((APP.parent / "static" / nome).read_bytes()).decode()
        html = html.replace(f"/static/{nome}", f"data:image/png;base64,{dados_img}")
    html = html.replace("/*__DADOS__*/null", json.dumps(dados, ensure_ascii=False, default=str).replace("</", "<\\/"))
    if "--exemplo" in sys.argv:
        html = html.replace("Prévia com dados de exemplo", "Prévia com dados de exemplo · empresas fictícias")
    # O publicador do Artifact envolve a página no próprio esqueleto: remove doctype/html/head/body.
    html = re.sub(r"<!doctype html>|</?html[^>]*>|</?head>|</?body>|<meta [^>]*>", "", html, flags=re.I)
    saida.write_text(html.strip() + "\n", encoding="utf-8")
    print(f"{saida} ({len(dados['linhas'])} negócios abertos, {len(dados['empresas'])} empresas)")


if __name__ == "__main__":
    main()
