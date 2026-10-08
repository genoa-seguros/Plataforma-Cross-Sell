import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crosssell import auth, qualidade, tabela, temperatura
from crosssell.normalize import AREA_LABEL, classificar_senioridade
from crosssell.potencial import criterios, influencia, praca, fatia_minima
from crosssell.config import VERTICAIS, VERTICAL_LABEL, get_settings
from crosssell.connectors import linkedin as lk
from crosssell.connectors import pipedrive as pd
from crosssell.db import SessionLocal, init_db
from crosssell.models import Atividade, Configuracao, Empresa, Melhoria, Negocio, Pessoa, SyncLog, Usuario
from crosssell.pipeline import (NOTICIAS_HORAS, QUALIDADE_DIAS, RECEITA_LOTE, ROTINA_DIAS, SITES_LOTE, recalcular,
                                registrar)

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


class _Guardada:
    """Resultado de um cálculo pesado, guardado no processo. Recalcula quando a rotina termina um passo
    (sync_log novo), quando alguém altera algo pela plataforma (POST etc., ver o middleware abaixo) ou
    depois de VALIDADE segundos."""

    VALIDADE = 900

    def __init__(self, calcular):
        self.calcular = calcular
        self.lock = threading.Lock()
        self.geracao, self.chave, self.em, self.valor = 0, None, 0.0, None

    def limpar(self) -> None:
        self.geracao += 1

    def obter(self, db: Session):
        ultimo_passo = db.scalar(select(func.max(SyncLog.id)).where(SyncLog.fim.is_not(None)))
        with self.lock:
            chave = (id(db.get_bind()), ultimo_passo, self.geracao)
            if chave != self.chave or time.monotonic() - self.em > self.VALIDADE:
                self.valor = self.calcular(db)
                self.chave, self.em = chave, time.monotonic()
            return self.valor


def _calcular_oportunidades(db: Session) -> tuple[list[dict], list[str]]:
    # O Potencial não fica gravado: para ordenar e paginar é preciso calcular todas
    itens = tabela.oportunidades(db, get_settings())
    return itens, sorted({u for o in itens for u in o["relacoes"]})


OPORTUNIDADES = _Guardada(_calcular_oportunidades)
# Duplicadas e sugestões de razão social, inteiras (a API devolve uma página de cada)
QUALIDADE = _Guardada(lambda db: (qualidade.duplicadas(db), qualidade.razao_social(db),
                                   _resumo_nomes_identicos(db), qualidade.suspeitas(db)))


@app.middleware("http")
async def _alteracao_invalida_oportunidades(request: Request, call_next):
    resp = await call_next(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and resp.status_code < 400:
        OPORTUNIDADES.limpar()
        QUALIDADE.limpar()
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


def acesso_qualidade(u: Usuario = Depends(usuario_atual)) -> Usuario:
    """Qualidade do cadastro: o master e quem tem o papel de head."""
    if u.papel not in ("master", "head"):
        raise HTTPException(403, "A Qualidade é só para o master e os heads.")
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
    return {"usuarios": [_usuario_json(x) for x in usuarios], "verticais": {v: VERTICAL_LABEL[v] for v in VERTICAIS},
            "pesos": {v: criterios()[v]["pesos"] for v in VERTICAIS}}  # critérios do Score (config/criterios.yaml)


@app.get("/api/tabela")
def api_tabela(db: Session = Depends(get_db), u: Usuario = Depends(usuario_atual)):
    return {"linhas": tabela.montar(db, get_settings()), **api_base(db, u)}


class FuncionariosIn(BaseModel):
    funcionarios: int | None


@app.post("/api/empresas/{empresa_id}/funcionarios")
def api_funcionarios(empresa_id: int, dados: FuncionariosIn, db: Session = Depends(get_db),
                     _u: Usuario = Depends(usuario_atual)):
    """Número de funcionários informado à mão: vale sobre o do LinkedIn por 180 dias. Vazio volta ao automático."""
    e = db.get(Empresa, empresa_id)
    if e is None:
        raise HTTPException(404, "Empresa não encontrada.")
    if dados.funcionarios is not None and not 0 < dados.funcionarios < 10_000_000:
        raise HTTPException(400, "Informe um número de funcionários válido.")
    if dados.funcionarios is None:
        if e.funcionarios_fonte == "manual":  # volta ao automático: a próxima leitura do LinkedIn preenche
            e.funcionarios_fonte, e.funcionarios_em = None, None
    else:
        e.funcionarios, e.funcionarios_fonte, e.funcionarios_em = dados.funcionarios, "manual", datetime.utcnow()
    db.commit()
    return {"funcionarios": e.funcionarios, "funcionariosFonte": e.funcionarios_fonte}


class CargoIn(BaseModel):
    cargo: str | None = None


@app.post("/api/pessoas/{pessoa_id}/cargo")
def api_cargo(pessoa_id: int, dados: CargoIn, db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual),
              client: pd.PipedriveClient = Depends(get_pipedrive)):
    """Cargo informado à mão (vale sobre o do LinkedIn, que só preenche cargo vazio). Também grava no Pipedrive."""
    p = db.get(Pessoa, pessoa_id)
    if p is None:
        raise HTTPException(404, "Pessoa não encontrada.")
    cargo = (dados.cargo or "").strip()[:120] or None
    p.cargo = cargo
    p.senioridade = classificar_senioridade(cargo or p.linkedin_headline) if (cargo or p.linkedin_headline) else None
    db.commit()
    no_pipedrive = False
    if p.pipedrive_person_id and cargo:
        try:
            client.atualizar_pessoa(int(p.pipedrive_person_id), {"job_title": cargo})
            no_pipedrive = True
        except Exception:  # o cargo fica na plataforma mesmo se o Pipedrive recusar
            no_pipedrive = False
    return {"cargo": p.cargo, "noPipedrive": no_pipedrive}


class LocalIn(BaseModel):
    cidade: str | None = None
    uf: str | None = None


@app.post("/api/empresas/{empresa_id}/local")
def api_local(empresa_id: int, dados: LocalIn, db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    """Cidade e UF informadas à mão: valem sobre Receita, Pipedrive e LinkedIn e decidem a praça de Saúde.
    Em branco volta ao automático (a próxima consulta à Receita preenche)."""
    e = db.get(Empresa, empresa_id)
    if e is None:
        raise HTTPException(404, "Empresa não encontrada.")
    cidade, uf = (dados.cidade or "").strip(), (dados.uf or "").strip().upper()
    if not cidade:
        if e.cidade_fonte == "manual":
            e.cidade_fonte, e.cidade_em, e.enriquecido_em = None, None, None  # a Receita volta a preencher
    else:
        if uf not in pd.UFS.values():
            raise HTTPException(400, "Escolha a UF.")
        e.cidade, e.uf, e.cidade_fonte, e.cidade_em = cidade, uf, "manual", datetime.utcnow()
    db.commit()
    return {"cidade": e.cidade, "uf": e.uf, "cidadeFonte": e.cidade_fonte, "local": tabela.local(e)}


@app.get("/api/empresas/{empresa_id}")
def api_empresa(empresa_id: int, db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    e = db.get(Empresa, empresa_id)
    if e is None:
        raise HTTPException(404, "Empresa não encontrada.")
    return {
        "id": e.id, "nome": e.razao_social, "cnpj": e.cnpj, "porte": e.porte, "cnae": e.cnae,
        "cidade": e.cidade, "uf": e.uf, "cidadeFonte": e.cidade_fonte, "praca": praca(e),
        "funcionarios": e.funcionarios, "funcionariosFonte": e.funcionarios_fonte,
        "funcionariosEm": e.funcionarios_em.isoformat(timespec="minutes") if e.funcionarios_em else None,
        "score": e.score_relacionamento,
        "comp": e.score_componentes,
        "linkedin": e.linkedin_url, "setor": e.setor,
        "dominios": [d for d in [e.dominio, *(e.dominios_extras or [])] if d],
        "pessoas": [{"id": p.id, "nome": p.nome, "cargo": p.cargo, "email": p.email, "score": p.score_relacionamento,
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


class PapelIn(BaseModel):
    papel: str


@app.post("/api/equipe/{usuario_id}/papel")
def api_papel(usuario_id: int, dados: PapelIn, db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    """Membro ou head (o head também acessa a Qualidade). O master não muda por aqui."""
    if dados.papel not in ("membro", "head"):
        raise HTTPException(400, "Papel deve ser membro ou head.")
    u = db.get(Usuario, usuario_id)
    if u is None:
        raise HTTPException(404, "Usuário não encontrado.")
    if u.papel == "master":
        raise HTTPException(400, "O papel do master não muda por aqui.")
    u.papel = dados.papel
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
    todas, relacoes = OPORTUNIDADES.obter(db)
    if empresa is not None:  # na ficha aparecem todas, com o que falta para as não analisadas
        return {"itens": [o for o in todas if o["empresa"]["id"] == empresa]}
    # Na tela, só as verticais com fluxo definido e as empresas já analisadas; o resto está na fila de análise
    visiveis = [o for o in todas if o["vertical"] in tabela.VERTICAIS_OPORTUNIDADES]
    itens = [o for o in visiveis if o["analisada"]]
    fila = {v: len({o["empresa"]["id"] for o in visiveis if o["vertical"] == v and not o["analisada"]})
            for v in tabela.VERTICAIS_OPORTUNIDADES}
    busca = q.strip().lower()
    filtradas = [o for o in itens
                 if (not vertical or o["vertical"] == vertical)
                 and (not busca or busca in (o["empresa"]["nome"] or "").lower())
                 and (not tipo or (tipo == "cliente") == o["cliente"])
                 and (not rel or (not o["relacoes"] if rel == "-" else rel in o["relacoes"]))]
    return {**qualidade.pagina(filtradas, pagina), "totalGeral": len(itens), "relacoes": relacoes,
            "verticais": list(tabela.VERTICAIS_OPORTUNIDADES), "fila": fila}


@app.get("/api/negocios")
def api_negocios(pagina: int = 1, funil: str = "", dono: str = "", q: str = "", db: Session = Depends(get_db),
                 _u: Usuario = Depends(usuario_atual)):
    """Negócios em aberto (LF, RE, Saúde, Pipo), uma página por vez, com quais empresas têm oportunidade analisada."""
    todos = tabela.negocios_abertos(db, get_settings())
    visiveis = [o for o in OPORTUNIDADES.obter(db)[0] if o["vertical"] in tabela.VERTICAIS_OPORTUNIDADES]
    com_op = {o["empresa"]["id"] for o in visiveis if o["analisada"]}
    # O que falta para a empresa entrar em Oportunidades (cidade, funcionários, setor): preencher à mão adianta
    falta: dict[int, list] = {}
    for o in visiveis:
        if not o["analisada"] and o["empresa"]["id"] not in com_op:
            falta.setdefault(o["empresa"]["id"], [])
            falta[o["empresa"]["id"]] += [x for x in o["faltando"] if x not in falta[o["empresa"]["id"]]]
    busca = q.strip().lower()
    filtrados = [n for n in todos if (not funil or n["funil"] == funil) and (not dono or n["dono"] == dono)
                 and (not busca or busca in ((n["empresa"] or {}).get("nome") or "").lower()
                      or busca in (n["titulo"] or "").lower())]
    p = qualidade.pagina(filtrados, pagina)
    for n in p["itens"]:
        n["temOportunidade"] = bool(n["empresa"]) and n["empresa"]["id"] in com_op
        n["faltaParaOportunidade"] = falta.get(n["empresa"]["id"], []) if n["empresa"] else []
    return {**p, "totalGeral": len(todos), "funis": sorted({n["funil"] for n in todos}),
            "donos": sorted({n["dono"] for n in todos if n["dono"]})}


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


def _resumo_nomes_identicos(db: Session) -> dict:
    grupos = qualidade.grupos_nome_identico(db)
    return {"grupos": len(grupos), "organizacoes": sum(len(m) - 1 for _, m in grupos)}


def _pagina_duplicadas(db: Session, pagina: int) -> dict:
    dup, _, nomes, _ = QUALIDADE.obter(db)
    p = qualidade.pagina(dup, pagina)
    return {"duplicadas": p.pop("itens"), "duplicadasPagina": p, "nomesIdenticos": nomes}


def _pagina_sugestoes(db: Session, pagina: int) -> dict:
    razao = QUALIDADE.obter(db)[1]
    p = qualidade.pagina(razao["sugestoes"], pagina)
    return {"sugestoes": p.pop("itens"), "sugestoesPagina": p, "semCnpj": razao["semCnpj"],
            "aguardandoReceita": razao["aguardandoReceita"]}


@app.get("/api/qualidade")
def api_qualidade(pagina: int = 1, db: Session = Depends(get_db), _m: Usuario = Depends(acesso_qualidade)):
    """Primeira página das duplicadas, uma página das sugestões de razão social (20 cada) e os cadastros suspeitos."""
    return {**_pagina_duplicadas(db, 1), **_pagina_sugestoes(db, pagina), "suspeitas": QUALIDADE.obter(db)[3]}


@app.get("/api/qualidade/duplicadas")
def api_qualidade_duplicadas(pagina: int = 1, db: Session = Depends(get_db), _m: Usuario = Depends(acesso_qualidade)):
    """Outra página das duplicadas."""
    return _pagina_duplicadas(db, pagina)


@app.get("/api/qualidade/sugestoes")
def api_qualidade_sugestoes(pagina: int = 1, db: Session = Depends(get_db), _m: Usuario = Depends(acesso_qualidade)):
    """Outra página das sugestões de razão social."""
    return _pagina_sugestoes(db, pagina)


class MesclarIn(BaseModel):
    manter_id: int
    mesclar_ids: list[int]


@app.post("/api/qualidade/mesclar")
def api_mesclar(dados: MesclarIn, db: Session = Depends(get_db), _m: Usuario = Depends(acesso_qualidade),
                client: pd.PipedriveClient = Depends(get_pipedrive)):
    try:
        return registrar(db, "qualidade-mesclar", qualidade.mesclar, db, client, dados.manter_id, dados.mesclar_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"O Pipedrive não aceitou a mesclagem: {exc}")


class NomesIdenticosIn(BaseModel):
    apos: str = ""


@app.post("/api/qualidade/mesclar-nomes-identicos")
def api_mesclar_nomes_identicos(dados: NomesIdenticosIn, db: Session = Depends(get_db),
                                _m: Usuario = Depends(acesso_qualidade),
                                client: pd.PipedriveClient = Depends(get_pipedrive)):
    """Um lote (10 grupos) da mesclagem por nome idêntico; a tela chama de novo com `apos` até `restantes` = 0."""
    try:
        return registrar(db, "qualidade-nomes", qualidade.mesclar_nomes_identicos, db, client, dados.apos)
    except Exception as exc:
        raise HTTPException(502, f"Não foi possível consultar o Pipedrive: {exc}")


class ExcluirIn(BaseModel):
    ids: list[int]


@app.post("/api/qualidade/excluir")
def api_excluir(dados: ExcluirIn, db: Session = Depends(get_db), _m: Usuario = Depends(acesso_qualidade),
                client: pd.PipedriveClient = Depends(get_pipedrive)):
    """Exclui no Pipedrive e na plataforma as organizações marcadas (até 50 por chamada; a tela manda em lotes)."""
    if len(dados.ids) > 50:
        raise HTTPException(400, "Mande no máximo 50 organizações por vez.")
    return registrar(db, "qualidade-excluir", qualidade.excluir, db, client, dados.ids)


class RazaoIn(BaseModel):
    empresa_id: int
    nome: str


@app.post("/api/qualidade/razao")
def api_razao(dados: RazaoIn, db: Session = Depends(get_db), _m: Usuario = Depends(acesso_qualidade),
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
def api_ignorar(dados: IgnorarIn, db: Session = Depends(get_db), _m: Usuario = Depends(acesso_qualidade)):
    qualidade.ignorar(db, dados.chave)
    return {"ok": True}


def _ia_json(db: Session) -> dict:
    s = get_settings()
    modelo, origem = temperatura.modelo_em_uso(db, s)
    return {"modelo": modelo, "origem": origem, "chave": bool(s.anthropic_api_key),
            "opcoes": [{"id": k, "nome": v} for k, v in temperatura.MODELOS.items()]}


@app.get("/api/ia")
def api_ia(db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    """Modelo da Claude usado na temperatura dos contatos e as opções da tela Equipe."""
    return _ia_json(db)


class IaIn(BaseModel):
    modelo: str


@app.post("/api/ia")
def api_ia_modelo(dados: IaIn, db: Session = Depends(get_db), u: Usuario = Depends(somente_master)):
    if dados.modelo not in temperatura.MODELOS:
        raise HTTPException(400, "Modelo não disponível.")
    c = db.get(Configuracao, temperatura.CHAVE_MODELO) or Configuracao(chave=temperatura.CHAVE_MODELO, valor="")
    c.valor, c.atualizado_por_id = dados.modelo, u.id
    db.add(c)
    db.commit()
    return _ia_json(db)


@app.get("/api/linkedin")
def api_linkedin(db: Session = Depends(get_db), _m: Usuario = Depends(somente_master)):
    s = get_settings()
    direto = lk.direto(s)
    return {"configurado": lk.configurado(s), "lote": s.linkedin_lote, **lk.situacao(db, s),
            "disparado": _ultimo(db, "linkedin" if direto else "linkedin-disparo"),
            "recebido": _ultimo(db, "linkedin" if direto else "linkedin-retorno")}


@app.get("/api/rotina")
def api_rotina(db: Session = Depends(get_db), _u: Usuario = Depends(usuario_atual)):
    """Como a rotina funciona (frequências e limites em vigor) e a última execução de cada passo, para a aba Rotina."""
    s = get_settings()
    direto = lk.direto(s)
    fontes = {"pipedrive": "pipedrive", "excluidas": "pipedrive-excluidas", "email": "email", "noticias": "noticias", "receita": "receita",
              "linkedinSites": "linkedin-sites", "cnpjSites": "cnpj-sites", "dominios": "dominios", "linkedin": "linkedin" if direto else "linkedin-disparo",
              "qualidade": "qualidade"}
    caixas = db.scalars(select(Usuario).where(Usuario.ativo.is_(True), Usuario.le_emails.is_(True),
                                              Usuario.senha_hash.is_not(None))).all()
    return {
        "minutos": s.rotina_minutos, "interna": s.rotina_interna, "dias": ROTINA_DIAS,
        "noticiasHoras": NOTICIAS_HORAS, "receitaLote": RECEITA_LOTE, "sitesLote": SITES_LOTE,
        "qualidadeDias": QUALIDADE_DIAS,
        "cidadesAlvo": len(criterios()["saude"]["metropoles"]), "verticaisOportunidades": list(tabela.VERTICAIS_OPORTUNIDADES),
        "funcionariosManualDias": lk.FUNCIONARIOS_MANUAL_DIAS,
        "email": {"configurado": bool(s.ms_tenant_id and s.ms_client_id), "temperatura": bool(s.anthropic_api_key),
                  "maxEmails": s.temperatura_max_emails, "caixas": len(caixas),
                  "recusadas": sum(u.leitura_erro == "recusada" for u in caixas)},
        "linkedin": {"configurado": lk.configurado(s), "direto": direto, "lote": s.linkedin_lote,
                     "limiteDia": s.linkedin_limite_dia, "validadeDias": s.linkedin_validade_dias,
                     "buscaDias": lk.BUSCA_VALIDADE.days, "prazoHoras": int(lk.PRAZO_PEDIDO.total_seconds() // 3600),
                     "ultimas24h": lk._pedidos_24h(db, datetime.utcnow()),
                     "salesNavigator": s.linkedin_sales_navigator, "fatiaMinima": fatia_minima(),
                     "cotas": [{"vertical": v, "grupo": g, "cota": c, "usadas24h": usadas.get((v, g), 0)}
                               for usadas in [lk._usadas_24h(db, datetime.utcnow())]
                               for (v, g), c in lk.cotas(s).items()]},
        "ultimos": {k: _ultimo(db, f) for k, f in fontes.items()},
    }


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
