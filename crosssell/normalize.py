"""Normalização de identificadores brasileiros e nomes para casamento entre sistemas."""

import re
import unicodedata
from datetime import date, datetime

_SUFIXOS_SOCIETARIOS = {
    "sa", "s/a", "ltda", "me", "epp", "eireli", "cia", "companhia", "limitada",
    "holding", "participacoes", "do", "da", "de", "dos", "das", "e", "brasil",
}

# Domínios de webmail nunca identificam uma empresa.
DOMINIOS_PUBLICOS = {
    "gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "yahoo.com.br",
    "icloud.com", "uol.com.br", "bol.com.br", "terra.com.br", "ig.com.br", "msn.com",
    "globo.com", "me.com",
}


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _dv(digitos: str, pesos: list[int]) -> int:
    resto = sum(int(d) * p for d, p in zip(digitos, pesos)) % 11
    return 0 if resto < 2 else 11 - resto


def cnpj_valido(cnpj: str) -> bool:
    c = so_digitos(cnpj)
    if len(c) != 14 or len(set(c)) == 1:
        return False
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    return _dv(c[:12], p1) == int(c[12]) and _dv(c[:13], [6] + p1) == int(c[13])


def cpf_valido(cpf: str) -> bool:
    c = so_digitos(cpf)
    if len(c) != 11 or len(set(c)) == 1:
        return False
    return (_dv(c[:9], list(range(10, 1, -1))) == int(c[9])
            and _dv(c[:10], list(range(11, 1, -1))) == int(c[10]))


def normalizar_cnpj(valor) -> str | None:
    c = so_digitos(valor)
    if len(c) < 14 and len(c) >= 12:  # planilhas costumam perder zeros à esquerda
        c = c.zfill(14)
    return c if cnpj_valido(c) else None


def normalizar_cpf(valor) -> str | None:
    c = so_digitos(valor).zfill(11) if so_digitos(valor) else ""
    return c if cpf_valido(c) else None


def normalizar_documento(valor) -> tuple[str | None, str | None]:
    """Retorna (cnpj, cpf) — no máximo um preenchido."""
    d = so_digitos(valor)
    if len(d) > 11:
        return normalizar_cnpj(d), None
    return None, normalizar_cpf(d)


def normalizar_nome_empresa(nome: str) -> str:
    t = sem_acento(nome or "").lower()
    t = re.sub(r"[^a-z0-9/ ]", " ", t.replace(".", ""))
    tokens = [tok for tok in t.split() if tok not in _SUFIXOS_SOCIETARIOS]
    return " ".join(tokens)


def normalizar_nome_pessoa(nome: str) -> str:
    t = sem_acento(nome or "").lower()
    t = re.sub(r"[^a-z ]", " ", t)
    return " ".join(t.split())


def normalizar_email(email) -> str | None:
    e = str(email or "").strip().lower()
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) else None


def dominio_email(email: str | None) -> str | None:
    e = normalizar_email(email)
    if not e:
        return None
    d = e.split("@", 1)[1]
    return None if d in DOMINIOS_PUBLICOS else d


def dominio_site(url: str | None) -> str | None:
    if not url:
        return None
    u = re.sub(r"^[a-z]+://", "", str(url).strip().lower())
    u = u.split("/", 1)[0].split(":", 1)[0]
    u = u.removeprefix("www.")
    return u or None


_CARGO_REGRAS = [
    ("socio", r"\b(socio|socia|fundador|founder|owner|proprietari|administrador)"),
    ("c_level", r"\b(ceo|cfo|cto|coo|cio|chro|cmo|presidente|chief|diretor geral|diretor presidente)"),
    ("diretor", r"\b(diretor|diretora|director|vp|vice[- ]presidente|head)"),
    ("gerente", r"\b(gerente|manager|coordenador|coordenadora|superintendente)"),
]


def classificar_senioridade(cargo: str | None) -> str | None:
    if not cargo:
        return None
    t = sem_acento(cargo).lower()
    for nivel, padrao in _CARGO_REGRAS:
        if re.search(padrao, t):
            return nivel
    return "outro"


def parse_data(valor) -> date | None:
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    s = str(valor).strip()[:10]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_numero(valor) -> float | None:
    if valor in (None, ""):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = re.sub(r"[^\d,.\-]", "", str(valor))
    if "," in s:  # formato brasileiro 1.234,56
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None
