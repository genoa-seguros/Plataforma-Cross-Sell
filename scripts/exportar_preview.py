"""Gera uma prévia estática (um único HTML) do painel a partir do banco atual.

Uso: DATABASE_URL=sqlite:///demo.db python scripts/exportar_preview.py saida.html [--exemplo]
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from crosssell.config import VERTICAIS, VERTICAL_LABEL, get_settings
from crosssell.db import SessionLocal, init_db
from crosssell.models import Empresa, Oportunidade
from crosssell.scoring.relacionamento import verticais_vigentes

TEMPLATE = Path(__file__).resolve().parents[1] / "crosssell" / "web" / "preview.html"


def _d(v):
    return v.isoformat() if v else None


def estado_vertical(e: Empresa, v: str) -> str:
    negs = [n for n in e.negocios if n.vertical == v] + [n for p in e.pessoas for n in p.negocios if n.vertical == v]
    if any(n.vigente for n in negs):
        return "cliente"
    if any(n.status == "aberto" for n in negs):
        return "aberto"
    if any(n.ex_cliente for n in negs):
        return "ex"
    return ""


def exportar(exemplo: bool) -> dict:
    init_db()
    db = SessionLocal()
    s = get_settings()
    empresas = {}
    prospeccao = 0
    clientes = 0
    mono = 0
    for e in db.scalars(select(Empresa)):
        vig = verticais_vigentes(e.negocios)
        if vig:
            clientes += 1
            mono += len(vig) == 1
        elif any(n.status == "aberto" for n in e.negocios):
            prospeccao += 1
        empresas[e.id] = {
            "nome": e.razao_social, "cnpj": e.cnpj, "porte": e.porte, "cnae": e.cnae, "cidade": e.cidade, "uf": e.uf,
            "funcionarios": e.funcionarios, "score": e.score_relacionamento, "comp": e.score_componentes,
            "estado": {v: estado_vertical(e, v) for v in VERTICAIS},
            "pessoas": [{
                "nome": p.nome, "cargo": p.cargo, "email": p.email, "score": p.score_relacionamento,
                "focal": p.ponto_focal, "fonte": p.fonte,
                "i90": (p.score_componentes or {}).get("interacoes_90d", 0),
                "dias": (p.score_componentes or {}).get("dias_ultima"),
                "usuarios": (p.score_componentes or {}).get("usuarios", []),
            } for p in sorted(e.pessoas, key=lambda p: p.score_relacionamento, reverse=True)],
            "negocios": [{
                "vertical": n.vertical, "fonte": n.fonte, "titulo": n.titulo or n.produto, "seguradora": n.seguradora,
                "status": n.status, "vigente": n.vigente, "ini": _d(n.inicio_vigencia), "fim": _d(n.fim_vigencia),
                "valor": n.valor,
            } for n in e.negocios],
        }
    ops = [{
        "id": o.id, "empresaId": o.empresa_id, "pessoa": o.pessoa.nome if o.pessoa else None,
        "cargo": o.pessoa.cargo if o.pessoa else None, "alvo": o.vertical_alvo, "atuais": o.verticais_atuais,
        "score": o.score, "comp": o.componentes, "motivos": o.motivos, "ponte": o.ponte_email,
        "responsavel": o.responsavel_email, "status": o.status,
    } for o in db.scalars(select(Oportunidade).order_by(Oportunidade.score.desc()))]
    return {
        "exemplo": exemplo, "geradoEm": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "verticais": {v: VERTICAL_LABEL[v] for v in VERTICAIS},
        "usuarios": {u["email"]: u["nome"] for u in s.usuarios()},
        "kpis": {"clientes": clientes, "prospeccao": prospeccao, "mono": mono},
        "empresas": empresas, "oportunidades": ops,
    }


def main():
    saida = Path(sys.argv[1])
    dados = exportar("--exemplo" in sys.argv)
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DADOS__*/null", json.dumps(dados, ensure_ascii=False).replace("</", "<\\/"))
    saida.write_text(html, encoding="utf-8")
    print(f"{saida} ({len(dados['oportunidades'])} oportunidades, {len(dados['empresas'])} empresas)")


if __name__ == "__main__":
    main()
