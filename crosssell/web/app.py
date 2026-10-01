from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from crosssell import auth, tabela
from crosssell.config import VERTICAIS, VERTICAL_LABEL, get_settings
from crosssell.connectors import linkedin as lk
from crosssell.connectors import pipedrive as pd
from crosssell.db import SessionLocal, init_db
from crosssell.models import Atividade, Empresa, Negocio, SyncLog, Usuario
from crosssell.pipeline import recalcular, registrar

AQUI = Path(__file__).parent
templates = Jinja2Templates(directory=AQUI / "templates")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Innoa Cross Sell", lifespan=lifespan)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_pipedrive():
    return pd.cliente(get_settings())


def usuario_atual(request: Request, db: Session = Depends(get_db)) -> Usuario:
    u = auth.usuario_da_sessao(db, request.cookies.get(auth.COOKIE))
    if u is None:
        raise HTTPException(401, "Faça login para continuar.")
    # Proteção contra CSRF: chamadas que alteram dados vêm do app (fetch com cabeçalho próprio).
    if request.method != "GET" and request.headers.get("x-cross-sell") != "1":
        raise HTTPException(403, "Requisição de origem não reconhecida.")
    return u


def somente_master(u: Usuario = Depends(usuario_atual)) -> Usuario:
    if u.papel != "master":
        raise HTTPException(403, "Só o usuário master pode gerenciar a equipe.")
    return u


# --- Páginas -------------------------------------------------------------

@app.get("/")
def inicio(request: Request, db: Session = Depends(get_db)):
    if auth.usuario_da_sessao(db, request.cookies.get(auth.COOKIE)) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(AQUI / "app.html")


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"erro": None})


@app.post("/login")
def login(request: Request, email: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    u = auth.autenticar(db, email, senha)
    if u is None:
        return templates.TemplateResponse(request, "login.html", {"erro": "E-mail ou senha incorretos.", "email": email},
                                          status_code=401)
    s = get_settings()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.abrir_sessao(db, u, s.sessao_dias), max_age=s.sessao_dias * 86400,
                    httponly=True, samesite="lax", secure=s.cookie_seguro)
    return resp


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    auth.encerrar_sessao(db, request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/convite/{token}")
def convite_form(request: Request, token: str, db: Session = Depends(get_db)):
    u = auth.usuario_do_convite(db, token)
    return templates.TemplateResponse(request, "convite.html", {"u": u, "erro": None}, status_code=200 if u else 404)


@app.post("/convite/{token}")
def convite(request: Request, token: str, senha: str = Form(...), confirmacao: str = Form(...),
            db: Session = Depends(get_db)):
    u = auth.usuario_do_convite(db, token)
    if u is None:
        return templates.TemplateResponse(request, "convite.html", {"u": None, "erro": None}, status_code=404)
    erro = auth.validar_senha(senha) or (None if senha == confirmacao else "As duas senhas não são iguais.")
    if erro:
        return templates.TemplateResponse(request, "convite.html", {"u": u, "erro": erro}, status_code=400)
    auth.aceitar_convite(db, u, senha)
    return RedirectResponse("/login", status_code=303)


# --- API -----------------------------------------------------------------

def _usuario_json(u: Usuario) -> dict:
    return {"id": u.id, "email": u.email, "nome": u.nome, "papel": u.papel, "ativo": u.ativo,
            "verticais": u.verticais, "lider": u.lider, "pipedrive": bool(u.pipedrive_user_id),
            "pendente": u.senha_hash is None}


@app.get("/api/eu")
def api_eu(u: Usuario = Depends(usuario_atual)):
    return _usuario_json(u)


@app.get("/api/tabela")
def api_tabela(db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    usuarios = db.scalars(select(Usuario).where(Usuario.ativo.is_(True)).order_by(Usuario.nome)).all()
    return {"linhas": tabela.montar(db, get_settings()), "usuarios": [_usuario_json(x) for x in usuarios],
            "verticais": {v: VERTICAL_LABEL[v] for v in VERTICAIS}}


@app.get("/api/empresas/{empresa_id}")
def api_empresa(empresa_id: int, db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    e = db.get(Empresa, empresa_id)
    if e is None:
        raise HTTPException(404, "Empresa não encontrada.")
    return {
        "id": e.id, "nome": e.razao_social, "cnpj": e.cnpj, "porte": e.porte, "cnae": e.cnae,
        "cidade": e.cidade, "uf": e.uf, "funcionarios": e.funcionarios, "score": e.score_relacionamento,
        "comp": e.score_componentes,
        "linkedin": e.linkedin_url, "setor": e.setor,
        "pessoas": [{"nome": p.nome, "cargo": p.cargo, "email": p.email, "score": p.score_relacionamento,
                     "fonte": p.fonte, "linkedin": p.linkedin_url, "headline": p.linkedin_headline,
                     "empresaAtual": p.linkedin_empresa_atual if lk.mudou_de_empresa(p) else None,
                     "focal": p.ponto_focal, "temperatura": p.temperatura, "temperaturaMotivo": p.temperatura_motivo,
                     "usuarios": (p.score_componentes or {}).get("usuarios", []),
                     "i90": (p.score_componentes or {}).get("interacoes_90d", 0)}
                    for p in sorted(e.pessoas, key=lambda p: p.score_relacionamento, reverse=True)],
        "negocios": [{"vertical": n.vertical, "produto": n.produto or n.titulo, "titulo": n.titulo, "fonte": n.fonte,
                      "status": n.status, "vigente": n.vigente, "etapa": n.etapa,
                      "ini": n.inicio_vigencia.isoformat() if n.inicio_vigencia else None,
                      "fim": n.fim_vigencia.isoformat() if n.fim_vigencia else None, "valor": n.valor}
                     for n in e.negocios],
        "noticias": [{"titulo": x.titulo, "fonte": x.fonte, "url": x.url,
                      "data": x.publicada_em.date().isoformat() if x.publicada_em else None} for x in e.noticias[:10]],
    }


class SaudeIn(BaseModel):
    cliente: bool


@app.post("/api/empresas/{empresa_id}/saude")
def api_saude(empresa_id: int, dados: SaudeIn, db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    if db.get(Empresa, empresa_id) is None:
        raise HTTPException(404, "Empresa não encontrada.")
    pd.marcar_saude(db, empresa_id, dados.cliente)
    db.commit()
    return {"ok": True}


class AtividadeIn(BaseModel):
    negocio_id: int
    pessoa_id: int
    assunto: str
    tipo: str = "task"
    vencimento: date
    responsavel_id: int
    nota: str | None = None


@app.post("/api/atividades")
def api_criar_atividade(dados: AtividadeIn, db: Session = Depends(get_db), u: Usuario = Depends(usuario_atual),
                        client: pd.PipedriveClient = Depends(get_pipedrive)):
    neg = db.get(Negocio, dados.negocio_id)
    resp = db.get(Usuario, dados.responsavel_id)
    if neg is None or resp is None or not resp.ativo:
        raise HTTPException(400, "Negócio ou responsável inválido.")
    try:
        at = pd.criar_atividade(db, client, negocio=neg, pessoa_id=dados.pessoa_id, assunto=dados.assunto.strip(),
                                vencimento=dados.vencimento, responsavel=resp, criada_por=u, tipo=dados.tipo,
                                nota=dados.nota)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": at.id, "pipedriveId": at.pipedrive_id}


class ConcluirIn(BaseModel):
    concluida: bool = True


@app.post("/api/atividades/{atividade_id}/concluir")
def api_concluir(atividade_id: int, dados: ConcluirIn, db: Session = Depends(get_db),
                 _u: Usuario = Depends(usuario_atual), client: pd.PipedriveClient = Depends(get_pipedrive)):
    at = db.get(Atividade, atividade_id)
    if at is None:
        raise HTTPException(404, "Atividade não encontrada.")
    pd.concluir_atividade(db, client, at, dados.concluida)
    return {"ok": True}


@app.get("/api/todos")
def api_todos(ref: date | None = None, db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    return tabela.todos(db, ref)


@app.get("/api/equipe")
def api_equipe(db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    return [_usuario_json(u) for u in db.scalars(select(Usuario).order_by(Usuario.ativo.desc(), Usuario.nome))]


class ConviteIn(BaseModel):
    email: str
    nome: str
    verticais: list[str] = []
    lider: list[str] = []


def _link(request: Request, token: str) -> str:
    return str(request.base_url).rstrip("/") + f"/convite/{token}"


@app.post("/api/equipe/convidar")
def api_convidar(dados: ConviteIn, request: Request, db: Session = Depends(get_db),
                 _m: Usuario = Depends(somente_master)):
    if "@" not in dados.email:
        raise HTTPException(400, "Informe um e-mail válido.")
    verticais = [v for v in dados.verticais if v in VERTICAIS]
    u, token = auth.convidar(db, dados.email, dados.nome, verticais, [v for v in dados.lider if v in verticais])
    return {"usuario": _usuario_json(u), "link": _link(request, token)}


@app.post("/api/equipe/{usuario_id}/desconvidar")
def api_desconvidar(usuario_id: int, db: Session = Depends(get_db), m: Usuario = Depends(somente_master)):
    u = db.get(Usuario, usuario_id)
    if u is None:
        raise HTTPException(404, "Usuário não encontrado.")
    if u.id == m.id:
        raise HTTPException(400, "Você não pode remover o próprio acesso.")
    auth.desconvidar(db, u)
    return _usuario_json(u)


@app.post("/api/equipe/{usuario_id}/reenviar")
def api_reenviar(usuario_id: int, request: Request, db: Session = Depends(get_db),
                 _m: Usuario = Depends(somente_master)):
    u = db.get(Usuario, usuario_id)
    if u is None:
        raise HTTPException(404, "Usuário não encontrado.")
    u, token = auth.convidar(db, u.email, u.nome, u.verticais, u.lider, papel=u.papel)
    return {"usuario": _usuario_json(u), "link": _link(request, token)}


@app.post("/api/importar")
async def api_importar(fonte: str = Form(...), arquivo: UploadFile = File(...), db: Session = Depends(get_db),
                       _u: Usuario = Depends(usuario_atual)):
    from crosssell.connectors import enriquecimento, planilhas

    fns = {"zeca": planilhas.importar_zeca, "linkedin": enriquecimento.importar_linkedin}
    if fonte not in fns:
        raise HTTPException(400, "Fonte inválida: use zeca ou linkedin.")
    res = registrar(db, fonte, fns[fonte], db, get_settings(), await arquivo.read(), arquivo.filename or "arquivo.csv")
    recalcular(db)
    return res


# --- LinkedIn (n8n + Linked API) -------------------------------------------

def _ultimo(db: Session, fonte: str) -> dict | None:
    log = db.scalar(select(SyncLog).where(SyncLog.fonte == fonte).order_by(SyncLog.id.desc()))
    return {"em": log.inicio.isoformat(timespec="minutes"), "registros": log.registros, "erro": log.erro} if log else None


@app.get("/api/linkedin")
def api_linkedin(db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    s = get_settings()
    pend = lk.alvos(db, s)
    sem = lk.alvos(db, s, com_endereco=False)
    return {"configurado": lk.configurado(s), "lote": s.linkedin_lote,
            "pendentes": {"empresas": sum(a["tipo"] == "empresa" for a in pend),
                          "pessoas": sum(a["tipo"] == "pessoa" for a in pend)},
            "semEndereco": {"empresas": sum(a["tipo"] == "empresa" for a in sem),
                            "pessoas": sum(a["tipo"] == "pessoa" for a in sem)},
            "disparado": _ultimo(db, "linkedin-disparo"), "recebido": _ultimo(db, "linkedin-retorno")}


@app.post("/api/linkedin/disparar")
def api_linkedin_disparar(db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    s = get_settings()
    if not lk.configurado(s):
        raise HTTPException(400, "Integração com o n8n não configurada (N8N_LINKEDIN_WEBHOOK_URL, N8N_TOKEN, PLATAFORMA_URL).")
    try:
        return registrar(db, "linkedin-disparo", lk.disparar, db, s)
    except Exception as exc:  # o n8n fora do ar não deve derrubar a tela
        raise HTTPException(502, f"O n8n não aceitou o disparo: {exc}")


@app.post("/api/integracoes/linkedin/resultados")
async def api_linkedin_resultados(request: Request, db: Session = Depends(get_db)):
    """Retorno do n8n. Autenticado pelo token compartilhado, não por sessão."""
    if not lk.token_valido(get_settings(), request.headers.get("authorization")):
        raise HTTPException(401, "Token inválido.")
    corpo = await request.json()
    itens = corpo if isinstance(corpo, list) else corpo.get("resultados", [corpo]) if isinstance(corpo, dict) else []
    res = registrar(db, "linkedin-retorno", lk.receber, db, [i for i in itens if isinstance(i, dict)])
    recalcular(db)
    return res
