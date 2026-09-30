from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crosssell.config import VERTICAIS, VERTICAL_LABEL, get_settings
from crosssell.db import SessionLocal, init_db
from crosssell.models import Empresa, Oportunidade, Pessoa, SyncLog
from crosssell.pipeline import recalcular, registrar
from crosssell.scoring.relacionamento import verticais_vigentes

@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Innoa Cross Sell", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(VERTICAIS=VERTICAIS, LABEL=VERTICAL_LABEL)
STATUS_OPORTUNIDADE = ("nova", "em_andamento", "convertida", "descartada")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _oportunidades(db: Session, vertical: str | None, status: str | None, limite: int = 200):
    q = select(Oportunidade).order_by(Oportunidade.score.desc()).limit(limite)
    if vertical:
        q = q.where(Oportunidade.vertical_alvo == vertical)
    if status:
        q = q.where(Oportunidade.status == status)
    return db.scalars(q).all()


def _serializar(o: Oportunidade) -> dict:
    return {
        "id": o.id, "empresa": o.empresa.razao_social if o.empresa else None, "empresa_id": o.empresa_id,
        "pessoa": o.pessoa.nome if o.pessoa else None, "vertical_alvo": o.vertical_alvo,
        "verticais_atuais": o.verticais_atuais, "score": o.score, "componentes": o.componentes,
        "motivos": o.motivos, "ponte": o.ponte_email, "responsavel": o.responsavel_email, "status": o.status,
    }


@app.get("/")
def painel(request: Request, vertical: str | None = None, status: str | None = "nova", db: Session = Depends(get_db)):
    empresas = db.scalars(select(Empresa)).all()
    clientes = [(e, verticais_vigentes(e.negocios)) for e in empresas]
    clientes = [(e, v) for e, v in clientes if v]
    dist = Counter(len(v) for _, v in clientes)
    por_vertical = {v: sum(1 for _, vs in clientes if v in vs) for v in VERTICAIS}
    abertas = dict(db.execute(
        select(Oportunidade.vertical_alvo, func.count()).where(Oportunidade.status == "nova")
        .group_by(Oportunidade.vertical_alvo)).all())
    ultimas = db.scalars(select(SyncLog).order_by(SyncLog.id.desc()).limit(8)).all()
    return templates.TemplateResponse(request, "painel.html", {
        "clientes": len(clientes), "dist": dist, "por_vertical": por_vertical, "abertas": abertas,
        "oportunidades": _oportunidades(db, vertical, status), "vertical": vertical, "status": status,
        "status_opcoes": STATUS_OPORTUNIDADE, "syncs": ultimas,
    })


@app.get("/empresas/{empresa_id}")
def empresa(request: Request, empresa_id: int, db: Session = Depends(get_db)):
    e = db.get(Empresa, empresa_id)
    if e is None:
        raise HTTPException(404)
    pessoas = sorted(e.pessoas, key=lambda p: p.score_relacionamento, reverse=True)
    estado = {}
    for v in VERTICAIS:
        negs = [n for n in e.negocios if n.vertical == v]
        negs += [n for p in e.pessoas for n in p.negocios if n.vertical == v]
        estado[v] = "cliente" if any(n.vigente for n in negs) else ("aberto" if any(n.status == "aberto" for n in negs) else "")
    ops = db.scalars(select(Oportunidade).where(Oportunidade.empresa_id == e.id).order_by(Oportunidade.score.desc())).all()
    return templates.TemplateResponse(request, "empresa.html", {
        "e": e, "pessoas": pessoas, "estado": estado, "oportunidades": ops, "status_opcoes": STATUS_OPORTUNIDADE,
    })


@app.post("/oportunidades/{op_id}/status")
def mudar_status(op_id: int, status: str = Form(...), voltar: str = Form("/"), db: Session = Depends(get_db)):
    o = db.get(Oportunidade, op_id)
    if o is None or status not in STATUS_OPORTUNIDADE:
        raise HTTPException(400)
    o.status = status
    db.commit()
    return RedirectResponse(voltar if voltar.startswith("/") else "/", status_code=303)


@app.post("/importar")
async def importar(fonte: str = Form(...), arquivo: UploadFile = File(...), db: Session = Depends(get_db)):
    from crosssell.connectors import enriquecimento, planilhas

    fns = {"zeca": planilhas.importar_zeca, "quiver": planilhas.importar_quiver,
           "linkedin": enriquecimento.importar_linkedin}
    if fonte not in fns:
        raise HTTPException(400, "fonte inválida")
    conteudo = await arquivo.read()
    registrar(db, fonte, fns[fonte], db, get_settings(), conteudo, arquivo.filename or "arquivo.csv")
    recalcular(db)
    return RedirectResponse("/", status_code=303)


@app.post("/recalcular")
def post_recalcular(db: Session = Depends(get_db)):
    recalcular(db)
    return RedirectResponse("/", status_code=303)


# --- API JSON (para integrar com Pipedrive, BI ou um agente) ---

@app.get("/api/oportunidades")
def api_oportunidades(vertical: str | None = None, status: str | None = None, limite: int = 100,
                      db: Session = Depends(get_db)):
    return [_serializar(o) for o in _oportunidades(db, vertical, status, limite)]


@app.get("/api/empresas/{empresa_id}")
def api_empresa(empresa_id: int, db: Session = Depends(get_db)):
    e = db.get(Empresa, empresa_id)
    if e is None:
        raise HTTPException(404)
    return {
        "id": e.id, "razao_social": e.razao_social, "cnpj": e.cnpj, "porte": e.porte, "cnae": e.cnae,
        "funcionarios": e.funcionarios, "score_relacionamento": e.score_relacionamento,
        "componentes": e.score_componentes, "verticais_cliente": sorted(verticais_vigentes(e.negocios)),
        "pontos_focais": [{"nome": p.nome, "cargo": p.cargo, "email": p.email, "score": p.score_relacionamento}
                          for p in db.scalars(select(Pessoa).where(Pessoa.empresa_id == e.id, Pessoa.ponto_focal))],
    }
