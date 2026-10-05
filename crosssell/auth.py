"""Login com senha, sessões e convites.

- Senhas: scrypt (stdlib) com sal aleatório.
- Sessão: token aleatório no cookie; no banco fica só o hash (revogável).
- Convite: o master gera um link de uso único; a pessoa define a senha nele.
- Esqueci a senha: link de uso único que vale 1 hora, enviado ao e-mail da pessoa
  (ou gerado pelo master na tela Equipe). Trocar a senha encerra as sessões abertas.
  Desconvidar desativa o usuário, encerra as sessões e para a leitura dos e-mails.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from crosssell.models import Sessao, Usuario

COOKIE = "cs_sessao"
CONVITE_DIAS = 7
REDEFINIR_VALIDADE = timedelta(hours=1)
REDEFINIR_INTERVALO = timedelta(minutes=2)  # evita disparos repetidos do mesmo pedido
SENHA_MIN = 10


def hash_senha(senha: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(senha.encode(), salt=sal, n=2**14, r=8, p=1)
    return f"scrypt${sal.hex()}${h.hex()}"


def conferir_senha(senha: str, armazenado: str | None) -> bool:
    if not armazenado or not armazenado.startswith("scrypt$"):
        return False
    _, sal, h = armazenado.split("$")
    calc = hashlib.scrypt(senha.encode(), salt=bytes.fromhex(sal), n=2**14, r=8, p=1)
    return hmac.compare_digest(calc.hex(), h)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validar_senha(senha: str) -> str | None:
    if len(senha) < SENHA_MIN:
        return f"A senha precisa ter pelo menos {SENHA_MIN} caracteres."
    return None


def autenticar(db: Session, email: str, senha: str) -> Usuario | None:
    u = db.scalar(select(Usuario).where(Usuario.email == email.strip().lower()))
    if u is None or not u.ativo or not conferir_senha(senha, u.senha_hash):
        return None
    return u


def abrir_sessao(db: Session, usuario: Usuario, dias: int) -> str:
    token = secrets.token_urlsafe(32)
    db.add(Sessao(token_hash=_hash_token(token), usuario_id=usuario.id,
                  expira=datetime.utcnow() + timedelta(days=dias)))
    db.commit()
    return token


def usuario_da_sessao(db: Session, token: str | None) -> Usuario | None:
    if not token:
        return None
    s = db.get(Sessao, _hash_token(token))
    if s is None or s.expira < datetime.utcnow() or not s.usuario.ativo:
        return None
    return s.usuario


def encerrar_sessao(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(Sessao).where(Sessao.token_hash == _hash_token(token)))
        db.commit()


def convidar(db: Session, email: str, nome: str, verticais: list[str], lider: list[str] | None = None,
             papel: str = "membro") -> tuple[Usuario, str]:
    """Cria (ou reativa) o usuário e devolve o token do link de convite."""
    email = email.strip().lower()
    u = db.scalar(select(Usuario).where(Usuario.email == email))
    if u is None:
        u = Usuario(email=email, nome=nome.strip() or email.split("@")[0], papel=papel)
        db.add(u)
    u.nome = nome.strip() or u.nome
    u.verticais = list(verticais)
    u.lider = list(lider or [])
    u.ativo = True
    token = secrets.token_urlsafe(24)
    u.convite_token = _hash_token(token)
    u.convite_expira = datetime.utcnow() + timedelta(days=CONVITE_DIAS)
    db.commit()
    return u, token


def usuario_do_convite(db: Session, token: str) -> Usuario | None:
    u = db.scalar(select(Usuario).where(Usuario.convite_token == _hash_token(token)))
    if u is None or not u.ativo or (u.convite_expira and u.convite_expira < datetime.utcnow()):
        return None
    return u


def aceitar_convite(db: Session, usuario: Usuario, senha: str) -> None:
    usuario.senha_hash = hash_senha(senha)
    usuario.convite_token = None
    usuario.convite_expira = None
    db.commit()


def desconvidar(db: Session, usuario: Usuario) -> None:
    usuario.ativo = False
    usuario.convite_token = None
    usuario.le_emails = False  # se for convidado de novo, o master liga outra vez
    db.execute(delete(Sessao).where(Sessao.usuario_id == usuario.id))
    db.commit()


def pedir_redefinicao(db: Session, email: str, agora: datetime | None = None) -> tuple[Usuario, str] | None:
    """Gera o link de nova senha. None se o e-mail não for de um usuário ativo com senha
    (a tela responde igual nos dois casos, para não revelar quem tem acesso)."""
    agora = agora or datetime.utcnow()
    u = db.scalar(select(Usuario).where(Usuario.email == email.strip().lower()))
    if u is None or not u.ativo or not u.senha_hash:
        return None
    if u.redefinir_expira and u.redefinir_expira - REDEFINIR_VALIDADE > agora - REDEFINIR_INTERVALO:
        return None  # pedido repetido em poucos minutos
    token = secrets.token_urlsafe(24)
    u.redefinir_token = _hash_token(token)
    u.redefinir_expira = agora + REDEFINIR_VALIDADE
    db.commit()
    return u, token


def usuario_da_redefinicao(db: Session, token: str) -> Usuario | None:
    u = db.scalar(select(Usuario).where(Usuario.redefinir_token == _hash_token(token)))
    if u is None or not u.ativo or not u.redefinir_expira or u.redefinir_expira < datetime.utcnow():
        return None
    return u


def redefinir_senha(db: Session, usuario: Usuario, senha: str) -> None:
    usuario.senha_hash = hash_senha(senha)
    usuario.redefinir_token = None
    usuario.redefinir_expira = None
    db.execute(delete(Sessao).where(Sessao.usuario_id == usuario.id))
    db.commit()
