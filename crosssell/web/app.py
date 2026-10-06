import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crosssell import auth, qualidade, tabela
from crosssell.normalize import AREA_LABEL
from crosssell.potencial import influencia
from crosssell.config import VERTICAIS, VERTICAL_LABEL, get_settings
from crosssell.connectors import linkedin as lk
from crosssell.connectors import pipedrive as pd
from crosssell.db import SessionLocal, init_db
from crosssell.models import Atividade, Empresa, Melhoria, Negocio, SyncLog, Usuario
from crosssell.pipeline import recalcular, registrar

AQUI = Path(__file__).parent
log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=AQUI / "templates")


def _rotina_interna(parar: threading.Event, minutos: int, limite_minutos: int) -> None:
    """Roda `crosssell rotina` dentro do próprio servidor, de hora em hora (ROTINA_INTERNA=true).
    Útil em hospedagens de um container só; com um agendador externo, deixe desligado."""
    import subprocess
    import sys

    if parar.wait(120):  # primeira rodada 2 min depois de subir
        return
    while True:
        try:
            subprocess.run([sys.executable, "-m", "crosssell.cli", "rotina"], check=False, timeout=limite_minutos * 60)
        except subprocess.TimeoutExpired:  # rodada travada: é encerrada para não parar a rotina para sempre
            log.warning("A rotina passou de %s minutos e foi encerrada; a próxima roda no horário.", limite_minutos)
        if parar.wait(max(minutos, 5) * 60):
            return


@asynccontextmanager
async def lifespan(_app):
    init_db()
    parar = threading.Event()
    s = get_settings()
    if s.rotina_interna:
        threading.Thread(target=_rotina_interna, args=(parar, s.rotina_minutos, s.rotina_limite_minutos),
                         daemon=True).start()
    yield
    parar.set()


app = FastAPI(title="Innoa Cross Sell", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)


class _CacheOportunidades:
    """A lista de Oportunidades é calculada inteira (o Potencial não fica gravado; para ordenar é preciso
    calcular todas) e guardada aqui. Recalcula quando a rotina termina um passo (sync_log novo), quando
    alguém altera algo pela plataforma (POST etc., ver o middleware abaixo) ou depois de VALIDADE segundos."""

    VALIDADE = 900

    def __init__(self):
        self.lock = threading.Lock()
        self.geracao, self.chave, self.em, self.itens, self.relacoes = 0, None, 0.0, [], []

    def limpar(self) -> None:
        self.geracao += 1

    def obter(self, db: Session) -> tuple[list[dict], list[str]]:
        ultimo_passo = db.scalar(select(func.max(SyncLog.id)).where(SyncLog.fim.is_not(None)))
        with self.lock:
            chave = (id(db.get_bind()), ultimo_passo, self.geracao)
            if chave != self.chave or time.monotonic() - self.em > self.VALIDADE:
                self.itens = tabela.oportunidades(db, get_settings())
                self.relacoes = sorted({u for o in self.itens for u in o["relacoes"]})
                self.chave, self.em = chave, time.monotonic()
            return self.itens, self.relacoes


OPORTUNIDADES = _CacheOportunidades()


@app.middleware("http")
async def _alteracao_invalida_oportunidades(request: Request, call_next):
    resp = await call_next(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and resp.status_code < 400:
        OPORTUNIDADES.limpar()
    return resp
app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")  # logo e favicon


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
    return templates.TemplateResponse(request, "login.html", {"erro": None, "aviso": None})


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


def _enviar_link_senha(request: Request, u: Usuario, token: str) -> bool:
    """Envia o link de nova senha pelo Microsoft 365. Sem remetente configurado, não envia
    (o master gera o link na tela Equipe)."""
    import html as _html
    import logging

    from crosssell.connectors.email_m365 import GraphClient

    s = get_settings()
    if not (s.email_remetente and s.ms_tenant_id and s.ms_client_id and s.ms_client_secret):
        return False
    link = str(request.base_url).rstrip("/") + f"/redefinir/{token}"
    corpo = (f"<p>Olá, {_html.escape(u.nome)}.</p><p>Para criar uma nova senha na plataforma Innoa Cross Sell, "
             f'acesse: <a href="{link}">{link}</a></p><p>O link vale 1 hora e só pode ser usado uma vez. '
             "Se você não pediu, ignore este e-mail: a senha atual continua valendo.</p>")
    try:
        GraphClient(s).enviar(s.email_remetente, u.email, "Innoa Cross Sell · nova senha", corpo)
        return True
    except Exception:  # falha no envio não pode revelar se o e-mail existe
        logging.getLogger(__name__).exception("Falha ao enviar o link de nova senha")
        return False


@app.get("/esqueci")
def esqueci_form(request: Request):
    return templates.TemplateResponse(request, "esqueci.html", {"enviado": False})


@app.post("/esqueci")
def esqueci(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    pedido = auth.pedir_redefinicao(db, email)
    if pedido:
        _enviar_link_senha(request, *pedido)
    return templates.TemplateResponse(request, "esqueci.html", {"enviado": True})  # mesma resposta sempre


@app.get("/redefinir/{token}")
def redefinir_form(request: Request, token: str, db: Session = Depends(get_db)):
    u = auth.usuario_da_redefinicao(db, token)
    return templates.TemplateResponse(request, "redefinir.html", {"u": u, "erro": None}, status_code=200 if u else 404)


@app.post("/redefinir/{token}")
def redefinir(request: Request, token: str, senha: str = Form(...), confirmacao: str = Form(...),
              db: Session = Depends(get_db)):
    u = auth.usuario_da_redefinicao(db, token)
    if u is None:
        return templates.TemplateResponse(request, "redefinir.html", {"u": None, "erro": None}, status_code=404)
    erro = auth.validar_senha(senha) or (None if senha == confirmacao else "As duas senhas não são iguais.")
    if erro:
        return templates.TemplateResponse(request, "redefinir.html", {"u": u, "erro": erro}, status_code=400)
    auth.redefinir_senha(db, u, senha)
    return templates.TemplateResponse(request, "login.html", {"erro": None, "email": u.email,
                                                              "aviso": "Senha alterada. Entre com a nova senha."})


# --- API -----------------------------------------------------------------

def _usuario_json(u: Usuario) -> dict:
    return {"id": u.id, "email": u.email, "nome": u.nome, "papel": u.papel, "ativo": u.ativo,
            "verticais": u.verticais, "lider": u.lider, "pipedrive": bool(u.pipedrive_user_id),
            "pendente": u.pendente, "leEmails": u.le_emails, "leituraErro": u.leitura_erro,
            "leituraEm": u.leitura_em.isoformat(timespec="minutes") if u.leitura_em else None}


@app.get("/api/eu")
def api_eu(u: Usuario = Depends(usuario_atual)):
    return _usuario_json(u)


@app.get("/api/base")
def api_base(db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    """Equipe ativa e verticais: o que toda tela precisa (sem montar a tabela)."""
    usuarios = db.scalars(select(Usuario).where(Usuario.ativo.is_(True)).order_by(Usuario.nome)).all()
    return {"usuarios": [_usuario_json(x) for x in usuarios], "verticais": {v: VERTICAL_LABEL[v] for v in VERTICAIS}}


@app.get("/api/tabela")
def api_tabela(db: Session = Depends(get_db), u: Usuario = Depends(usuario_atual)):
    return {"linhas": tabela.montar(db, get_settings()), **api_base(db, u)}


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
                     "area": AREA_LABEL.get(tabela.area_pessoa(p) or ""),
                     "influencia": influencia(p)["score"],
                     "fonte": p.fonte, "linkedin": p.linkedin_url, "headline": p.linkedin_headline,
                     "empresaAtual": p.linkedin_empresa_atual if lk.mudou_de_empresa(p) else None,
                     "focal": p.ponto_focal, "temperatura": p.temperatura, "temperaturaMotivo": p.temperatura_motivo,
                     "usuarios": (p.score_componentes or {}).get("usuarios", []),
                     "i90": (p.score_componentes or {}).get("interacoes_90d", 0)}
                    for p in sorted(e.pessoas, key=lambda p: influencia(p)["score"], reverse=True)],
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
    negocio_id: int | None = None
    empresa_id: int | None = None  # sem negócio (aba Oportunidades)
    pessoa_id: int | None = None
    assunto: str
    tipo: str = "task"
    vencimento: date
    responsavel_id: int
    nota: str | None = None


@app.post("/api/atividades")
def api_criar_atividade(dados: AtividadeIn, db: Session = Depends(get_db), u: Usuario = Depends(usuario_atual),
                        client: pd.PipedriveClient = Depends(get_pipedrive)):
    neg = db.get(Negocio, dados.negocio_id) if dados.negocio_id else None
    emp = db.get(Empresa, dados.empresa_id) if dados.empresa_id else None
    resp = db.get(Usuario, dados.responsavel_id)
    if (neg is None and emp is None) or resp is None or not resp.ativo:
        raise HTTPException(400, "Negócio/empresa ou responsável inválido.")
    try:
        at = pd.criar_atividade(db, client, negocio=neg, empresa=emp, pessoa_id=dados.pessoa_id,
                                assunto=dados.assunto.strip(), vencimento=dados.vencimento, responsavel=resp,
                                criada_por=u, tipo=dados.tipo, nota=dados.nota)
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


class LeituraIn(BaseModel):
    ler: bool


@app.post("/api/equipe/{usuario_id}/emails")
def api_leitura_emails(usuario_id: int, dados: LeituraIn, db: Session = Depends(get_db),
                       _m: Usuario = Depends(somente_master)):
    """Liga ou desliga a leitura da caixa de e-mail. A pessoa também precisa estar no grupo da Access
    Policy do Microsoft 365; senão a leitura é recusada e a tela mostra "recusada"."""
    u = db.get(Usuario, usuario_id)
    if u is None:
        raise HTTPException(404, "Usuário não encontrado.")
    if dados.ler and (not u.ativo or u.pendente):
        raise HTTPException(400, "A leitura só pode ser ligada depois que a pessoa entrar na plataforma.")
    u.le_emails = dados.ler
    if not dados.ler:
        u.leitura_erro = None
    db.commit()
    return _usuario_json(u)


@app.post("/api/equipe/{usuario_id}/desconvidar")
def api_desconvidar(usuario_id: int, db: Session = Depends(get_db), m: Usuario = Depends(somente_master)):
    u = db.get(Usuario, usuario_id)
    if u is None:
        raise HTTPException(404, "Usuário não encontrado.")
    if u.id == m.id:
        raise HTTPException(400, "Você não pode remover o próprio acesso.")
    auth.desconvidar(db, u)
    return _usuario_json(u)


@app.post("/api/equipe/{usuario_id}/nova-senha")
def api_nova_senha(usuario_id: int, request: Request, db: Session = Depends(get_db),
                   _m: Usuario = Depends(somente_master)):
    """Link de nova senha gerado pelo master (quando o e-mail não chega)."""
    u = db.get(Usuario, usuario_id)
    if u is None or not u.ativo or not u.senha_hash:
        raise HTTPException(400, "Só para quem já tem acesso ativo; para convites pendentes, gere um novo convite.")
    u.redefinir_expira = None  # o master pode gerar sem esperar o intervalo
    u, token = auth.pedir_redefinicao(db, u.email)
    return {"usuario": _usuario_json(u), "link": str(request.base_url).rstrip("/") + f"/redefinir/{token}"}


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


@app.get("/api/oportunidades")
def api_oportunidades(pagina: int = 1, vertical: str = "", tipo: str = "", rel: str = "", q: str = "",
                      empresa: int | None = None, db: Session = Depends(get_db),
                      _u: Usuario = Depends(usuario_atual)):
    """Uma página (20) das oportunidades com os filtros da tela. Com `empresa`, todas as daquela empresa (ficha).
    rel: e-mail de quem da equipe tem relação, ou "-" para sem relação. tipo: cliente | lead."""
    itens, relacoes = OPORTUNIDADES.obter(db)
    if empresa is not None:
        return {"itens": [o for o in itens if o["empresa"]["id"] == empresa]}
    busca = q.strip().lower()
    filtradas = [o for o in itens
                 if (not vertical or o["vertical"] == vertical)
                 and (not busca or busca in (o["empresa"]["nome"] or "").lower())
                 and (not tipo or (tipo == "cliente") == o["cliente"])
                 and (not rel or (not o["relacoes"] if rel == "-" else rel in o["relacoes"]))]
    return {**qualidade.pagina(filtradas, pagina), "totalGeral": len(itens), "relacoes": relacoes}


PRIORIDADES = ("alta", "media", "baixa")
SITUACOES = ("nova", "andamento", "feita", "descartada")


def _melhoria_json(x: Melhoria) -> dict:
    return {"id": x.id, "titulo": x.titulo, "descricao": x.descricao, "prioridade": x.prioridade,
            "situacao": x.situacao, "autor": x.autor.nome, "autorId": x.autor_id,
            "criadaEm": x.criada_em.isoformat(timespec="minutes"), "atualizadaEm": x.atualizada_em.isoformat(timespec="minutes")}


@app.get("/api/melhorias")
def api_melhorias(db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    itens = db.scalars(select(Melhoria).order_by(Melhoria.criada_em.desc())).all()
    return [_melhoria_json(x) for x in itens]


class MelhoriaIn(BaseModel):
    titulo: str | None = None
    descricao: str | None = None
    prioridade: str | None = None
    situacao: str | None = None


@app.post("/api/melhorias")
def api_nova_melhoria(dados: MelhoriaIn, db: Session = Depends(get_db), u: Usuario = Depends(usuario_atual)):
    titulo = (dados.titulo or "").strip()
    if not titulo:
        raise HTTPException(400, "Escreva a melhoria.")
    x = Melhoria(titulo=titulo[:300], descricao=(dados.descricao or "").strip()[:4000] or None,
                 prioridade=dados.prioridade if dados.prioridade in PRIORIDADES else "media", autor_id=u.id)
    db.add(x)
    db.commit()
    return _melhoria_json(x)


@app.post("/api/melhorias/{melhoria_id}")
def api_editar_melhoria(melhoria_id: int, dados: MelhoriaIn, db: Session = Depends(get_db),
                        u: Usuario = Depends(usuario_atual)):
    x = db.get(Melhoria, melhoria_id)
    if x is None:
        raise HTTPException(404, "Melhoria não encontrada.")
    if u.papel != "master" and x.autor_id != u.id:
        raise HTTPException(403, "Só quem anotou ou o master pode alterar.")
    if dados.titulo is not None and dados.titulo.strip():
        x.titulo = dados.titulo.strip()[:300]
    if dados.descricao is not None:
        x.descricao = dados.descricao.strip()[:4000] or None
    if dados.prioridade in PRIORIDADES:
        x.prioridade = dados.prioridade
    if dados.situacao in SITUACOES:
        if dados.situacao != x.situacao and u.papel != "master":
            raise HTTPException(403, "Só o master muda a situação.")
        x.situacao = dados.situacao
    db.commit()
    return _melhoria_json(x)


@app.get("/api/qualidade")
def api_qualidade(pagina: int = 1, db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    """Duplicadas inteiras (são poucas; a tela pagina) e uma página das sugestões de razão social."""
    return {"duplicadas": qualidade.duplicadas(db), **qualidade.razao_social(db, pagina)}


@app.get("/api/qualidade/sugestoes")
def api_qualidade_sugestoes(pagina: int = 1, db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    """Outra página das sugestões de razão social, sem refazer a busca de duplicadas."""
    return qualidade.razao_social(db, pagina)


class MesclarIn(BaseModel):
    manter_id: int
    mesclar_ids: list[int]


@app.post("/api/qualidade/mesclar")
def api_mesclar(dados: MesclarIn, db: Session = Depends(get_db), _m: Usuario = Depends(somente_master),
                client: pd.PipedriveClient = Depends(get_pipedrive)):
    try:
        return registrar(db, "qualidade-mesclar", qualidade.mesclar, db, client, dados.manter_id, dados.mesclar_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"O Pipedrive não aceitou a mesclagem: {exc}")


class RazaoIn(BaseModel):
    empresa_id: int
    nome: str


@app.post("/api/qualidade/razao")
def api_razao(dados: RazaoIn, db: Session = Depends(get_db), _m: Usuario = Depends(somente_master),
              client: pd.PipedriveClient = Depends(get_pipedrive)):
    try:
        qualidade.aplicar_razao(db, client, dados.empresa_id, dados.nome)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"O Pipedrive não aceitou a alteração: {exc}")
    return {"ok": True}


class IgnorarIn(BaseModel):
    chave: str


@app.post("/api/qualidade/ignorar")
def api_ignorar(dados: IgnorarIn, db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    qualidade.ignorar(db, dados.chave)
    return {"ok": True}


@app.get("/api/linkedin")
def api_linkedin(db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    s = get_settings()
    direto = lk.direto(s)
    return {"configurado": lk.configurado(s), "lote": s.linkedin_lote, **lk.situacao(db, s),
            "disparado": _ultimo(db, "linkedin" if direto else "linkedin-disparo"),
            "recebido": _ultimo(db, "linkedin" if direto else "linkedin-retorno")}


class EnderecoIn(BaseModel):
    id_alvo: str
    url: str


@app.post("/api/linkedin/endereco")
def api_linkedin_endereco(dados: EnderecoIn, db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    try:
        lk.definir_endereco(db, dados.id_alvo, dados.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/linkedin/disparar")
def api_linkedin_disparar(db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    s = get_settings()
    if not lk.configurado(s):
        raise HTTPException(400, "LinkedIn não configurado (LINKED_API_TOKEN e LINKED_API_IDENTIFICATION_TOKEN).")
    try:
        if lk.direto(s):
            return registrar(db, "linkedin", lk.executar, db, s)
        return registrar(db, "linkedin-disparo", lk.disparar, db, s)
    except Exception as exc:  # serviço fora do ar não deve derrubar a tela
        raise HTTPException(502, f"A Linked API não aceitou o pedido: {exc}")


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
