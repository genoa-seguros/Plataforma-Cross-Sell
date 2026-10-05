"""Score de Influência (a pessoa) e Potencial do negócio (critérios de cada vertical).

Influência = relacionamento (e-mails com a equipe, ajustado pela temperatura) + hierarquia
             do cargo. Quanto mais alto o cargo e mais forte a relação, maior.

Potencial  = critérios da vertical, pesos e palavras em config/criterios.yaml:
  Saúde   influência · funcionários · qualificação do time · localização · RH estruturado
  LF      influência · encaixe do produto (E&O: serviço intelectual; D&O: gestão profissional,
          aporte, fundo; Cyber: CTO/DPO)
  RE      influência · perfil (galpões, indústrias, transportadoras) · porte
  Canais  só influência

Cada critério devolve um valor de 0 a 1 e uma frase para a coluna "Por quê", com sinal
"+" (a favor), "-" (contra) ou "?" (ainda sem dado).
"""

import math
import re
from functools import lru_cache
from pathlib import Path

import yaml

from crosssell.models import Empresa, Negocio, Pessoa
from crosssell.normalize import classificar_area, classificar_senioridade, sem_acento

ARQUIVO = Path(__file__).resolve().parents[1] / "config" / "criterios.yaml"
AJUSTE_TEMPERATURA = {"muita": 0.15, "media": 0.0, "pouca": -0.15}
SEM_DADO = 0.4  # critério ainda sem informação: nem ajuda nem derruba


@lru_cache(maxsize=4)
def _ler(mtime: float) -> dict:
    return yaml.safe_load(ARQUIVO.read_text(encoding="utf-8"))


def criterios() -> dict:
    """Relido quando o arquivo muda: editar config/criterios.yaml vale sem reiniciar."""
    return _ler(ARQUIVO.stat().st_mtime)


def _norm(texto: str | None) -> str:
    return sem_acento(texto or "").lower()


def _acha(texto: str, palavras: list[str]) -> str | None:
    """Primeira palavra da lista encontrada no texto (início de palavra; siglas curtas, palavra inteira)."""
    for p in palavras:
        p = p.strip()
        fim = r"\b" if len(p) <= 3 else ""
        if p and re.search(r"\b" + re.escape(p) + fim, texto):
            return p
    return None


def texto_empresa(e: Empresa | None) -> str:
    if e is None:
        return ""
    return _norm(" ".join(x for x in (e.setor, e.cnae, e.descricao, e.razao_social, e.nome_fantasia) if x))


def _setor_rotulo(e: Empresa) -> str:
    return e.setor or (e.cnae.split(" - ", 1)[-1] if e.cnae else "") or "setor"


# --- Influência --------------------------------------------------------------

def hierarquia(p: Pessoa) -> tuple[float, str]:
    nivel = p.senioridade or classificar_senioridade(p.cargo or p.linkedin_headline)
    tabela = criterios()["influencia"]["hierarquia"]
    return float(tabela.get(nivel or "desconhecido", tabela["desconhecido"])), nivel or "desconhecido"


def relacionamento(p: Pessoa) -> float:
    base = (p.score_relacionamento or 0) / 100
    return max(0.0, min(1.0, base + AJUSTE_TEMPERATURA.get(p.temperatura or "", 0.0)))


def influencia(p: Pessoa | None) -> dict:
    if p is None:
        return {"score": 0.0, "relacionamento": 0.0, "hierarquia": 0.0, "nivel": None}
    pesos = criterios()["influencia"]["pesos"]
    rel, (hier, nivel) = relacionamento(p), hierarquia(p)
    total = pesos["relacionamento"] + pesos["hierarquia"]
    score = 100 * (pesos["relacionamento"] * rel + pesos["hierarquia"] * hier) / total
    return {"score": round(score, 1), "relacionamento": round(rel, 3), "hierarquia": hier, "nivel": nivel}


def melhor_contato(e: Empresa | None) -> Pessoa | None:
    pessoas = list(e.pessoas) if e else []
    return max(pessoas, key=lambda p: influencia(p)["score"], default=None)


# --- Critérios -----------------------------------------------------------------

def _crit(nome: str, valor: float, texto: str | None, sinal: str) -> dict:
    return {"nome": nome, "valor": round(max(0.0, min(1.0, valor)), 3), "texto": texto, "sinal": sinal}


def _tamanho(n: Negocio | None, e: Empresa | None, saude: bool) -> tuple[int | None, str]:
    if saude and n is not None and n.vidas:
        return n.vidas, "vidas"
    if e is not None and e.funcionarios:
        return e.funcionarios, "funcionários"
    return None, ""


def _escala(numero: int) -> float:
    return max(0.0, min(1.0, math.log10(max(numero, 1)) / 3))  # 10 → 0,33 · 100 → 0,67 · 1.000+ → 1


def c_funcionarios(n, e, saude=True, nome="funcionarios") -> dict:
    numero, unidade = _tamanho(n, e, saude)
    if numero is None:
        return _crit(nome, SEM_DADO, "nº de funcionários desconhecido", "?")
    fmt = f"{numero:,}".replace(",", ".")
    if numero >= 100:
        return _crit(nome, _escala(numero), f"{fmt} {unidade}: empresa grande", "+")
    if numero < 30:
        return _crit(nome, _escala(numero), f"só {fmt} {unidade}", "-")
    return _crit(nome, _escala(numero), None, "")


def c_qualificacao(e: Empresa | None) -> dict:
    c, t = criterios()["saude"], texto_empresa(e)
    if e is not None and e.investida:
        return _crit("qualificacao", 1.0, "empresa investida (venture capital): time qualificado", "+")
    if _acha(t, c.get("qualificacao_baixa_sempre", [])):
        return _crit("qualificacao", 0.2, f"{_setor_rotulo(e)}: costuma contratar plano mais simples", "-")
    alta, baixa = _acha(t, c["qualificacao_alta"]), _acha(t, c["qualificacao_baixa"])
    if alta:
        return _crit("qualificacao", 1.0, f"{_setor_rotulo(e)}: time qualificado, plano melhor", "+")
    if baixa:
        return _crit("qualificacao", 0.2, f"{_setor_rotulo(e)}: costuma contratar plano mais simples", "-")
    return _crit("qualificacao", SEM_DADO if not t else 0.5, None if t else "setor desconhecido", "" if t else "?")


def _cidade(e: Empresa | None) -> tuple[str, str]:
    """Cidade e UF a partir de "São Paulo", "SAO PAULO" (Receita) ou "Joinville, SC" (LinkedIn)."""
    if e is None or not e.cidade:
        return "", (e.uf or "") if e else ""
    partes = [x.strip() for x in e.cidade.split(",")]
    uf = e.uf or (partes[1] if len(partes) > 1 and len(partes[1]) == 2 else "")
    return _norm(partes[0]), uf.upper()


def c_localizacao(e: Empresa | None) -> dict:
    cidade, uf = _cidade(e)
    if not cidade:
        return _crit("localizacao", SEM_DADO, "localização desconhecida", "?")
    rotulo = e.cidade.split(",")[0].strip().title() + (f"/{uf}" if uf else "")
    if cidade in [_norm(x) for x in criterios()["saude"]["metropoles"]]:
        return _crit("localizacao", 1.0, None, "")
    return _crit("localizacao", 0.2, f"{rotulo}: interior, Unimed local costuma ser forte", "-")


def c_rh(e: Empresa | None) -> dict:
    c = criterios()["saude"]
    rh = [p for p in (e.pessoas if e else []) if classificar_area(p.cargo) == "rh" or classificar_area(p.linkedin_headline) == "rh"]
    if not rh:
        return _crit("rh", 0.3, "ninguém de RH mapeado ainda", "?")
    melhor, texto_melhor = 0.0, ""
    for p in rh:
        cargo = p.cargo or p.linkedin_headline or ""
        t = _norm(f"{p.cargo or ''} {p.linkedin_headline or ''}")
        nivel = p.senioridade or classificar_senioridade(cargo)
        valor = {"socio": 1.0, "c_level": 1.0, "diretor": 1.0, "gerente": 0.8}.get(nivel or "", 0.5)
        if _acha(t, c["rh_basico"]) and nivel not in ("diretor", "c_level", "gerente"):
            valor = 0.2
        if _acha(t, c["rh_bom"]):
            valor = min(1.0, valor + 0.15)
        if valor > melhor:
            melhor, texto_melhor = valor, f"{p.nome} ({cargo})"
    if melhor >= 0.8:
        return _crit("rh", melhor, f"RH estruturado: {texto_melhor}", "+")
    if melhor <= 0.2:
        return _crit("rh", melhor, f"RH só operacional: {texto_melhor}", "-")
    return _crit("rh", melhor, f"RH: {texto_melhor}", "")


def _cargos(e: Empresa | None) -> str:
    return _norm(" | ".join(f"{p.cargo or ''} {p.linkedin_headline or ''}" for p in (e.pessoas if e else [])))


def _noticias(e: Empresa | None) -> str:
    return _norm(" | ".join(x.titulo for x in (e.noticias if e else [])))


def produto_lf(produto: str | None) -> str | None:
    t = _norm(produto)
    if re.search(r"\b(d&o|do|imi)\b|d & o", t):
        return "do"
    if "cyber" in t or "ciber" in t:
        return "cyber"
    if re.search(r"\b(e&o|eo)\b|medmal|profissional|responsabilidade civil", t):
        return "eo"
    return None


def fit_eo(e: Empresa | None) -> tuple[float, str | None, str]:
    achou = _acha(texto_empresa(e), criterios()["linhas_financeiras"]["eo"])
    if achou:
        return 1.0, f"E&O: presta serviço intelectual ({_setor_rotulo(e)})", "+"
    return (0.2, "E&O: não parece serviço intelectual", "-") if texto_empresa(e) else (SEM_DADO, "setor desconhecido", "?")


def fit_do(e: Empresa | None) -> tuple[float, str | None, str]:
    c, t = criterios()["linhas_financeiras"], texto_empresa(e)
    if _acha(t, c["fundo"]):
        return 1.0, "fundo/gestora: oferecer IMI (D&O + E&O)", "+"
    sinais, valor = [], 0.15
    if e is not None and e.investida:
        sinais.append("recebeu venture capital"); valor += 0.4
    if _acha(_noticias(e), c["aporte"]):
        sinais.append("notícia de aporte/conselho"); valor += 0.3
    if e is not None and "anonima" in _norm(e.natureza_juridica):
        sinais.append("S.A."); valor += 0.25
    executivos = re.search(r"\b(cfo|coo|cto|chief|diretor financeiro|diretora financeira|conselh)", _cargos(e))
    if executivos:
        sinais.append("diretoria executiva"); valor += 0.2
    if e is not None and (e.funcionarios or 0) >= 200:
        sinais.append("porte"); valor += 0.1
    if sinais:
        return min(1.0, valor), "D&O: gestão profissional (" + ", ".join(sinais) + ")", "+"
    return valor, "D&O: sem sinal de gestão profissional (pode ser familiar)", "-"


def fit_cyber(e: Empresa | None) -> tuple[float, str | None, str]:
    c = criterios()["linhas_financeiras"]
    cargo = _acha(_cargos(e), c["cyber_cargos"])
    setor = _acha(texto_empresa(e), c["cyber_setor"])
    if cargo:
        return min(1.0, 0.75 + (0.25 if setor else 0)), f"Cyber: tem {cargo.upper() if len(cargo) <= 4 else cargo} na empresa", "+"
    if setor:
        return 0.6, f"Cyber: setor com muitos dados ({_setor_rotulo(e)})", "+"
    return 0.3, "Cyber: nenhum CTO ou DPO mapeado", "?"


FITS = {"eo": ("E&O", fit_eo), "do": ("D&O", fit_do), "cyber": ("Cyber", fit_cyber)}


def c_produto_lf(e: Empresa | None, produto: str | None) -> dict:
    alvo = produto_lf(produto)
    if alvo:
        v, txt, sinal = FITS[alvo][1](e)
        return _crit("produto", v, txt, sinal)
    melhores = sorted(((f(e), nome) for nome, f in FITS.values()), key=lambda x: -x[0][0])
    (v, txt, sinal), nome = melhores[0]
    return _crit("produto", v, txt or f"melhor encaixe: {nome}", sinal or "+")


def c_perfil_re(e: Empresa | None) -> dict:
    t = texto_empresa(e)
    achou = _acha(t, criterios()["ramos_elementares"]["perfil_forte"])
    if achou:
        return _crit("perfil", 1.0, f"{_setor_rotulo(e)}: galpão/indústria/frota move o ponteiro", "+")
    if e is not None and (e.funcionarios or 0) >= 200:
        return _crit("perfil", 0.6, "empresa grande em escritório: empresarial e seguro fiança", "+")
    if not t:
        return _crit("perfil", SEM_DADO, "setor desconhecido", "?")
    return _crit("perfil", 0.35, "escritório: incêndio/empresarial básico", "")


def c_influencia(p: Pessoa | None) -> dict:
    inf = influencia(p)
    if p is None:
        return _crit("influencia", 0.0, "sem contato no negócio", "-")
    return _crit("influencia", inf["score"] / 100, None, "")


# --- Potencial -------------------------------------------------------------------

def potencial(vertical: str | None, e: Empresa | None, p: Pessoa | None, n: Negocio | None = None,
              produto: str | None = None) -> dict:
    """Potencial do negócio (0–100) pelos critérios da vertical, com os critérios usados."""
    if vertical == "saude":
        crit = [c_influencia(p), c_funcionarios(n, e), c_qualificacao(e), c_localizacao(e), c_rh(e)]
    elif vertical == "linhas_financeiras":
        crit = [c_influencia(p), c_produto_lf(e, produto or (n.produto if n else None))]
    elif vertical == "ramos_elementares":
        crit = [c_influencia(p), c_perfil_re(e), c_funcionarios(n, e, saude=False, nome="porte")]
    else:
        crit = [c_influencia(p)]
    pesos = criterios().get(vertical, {}).get("pesos", {"influencia": 100})
    total = sum(pesos.get(c["nome"], 0) for c in crit) or 1
    for c in crit:
        c["peso"] = pesos.get(c["nome"], 0)
    score = 100 * sum(c["peso"] * c["valor"] for c in crit) / total
    return {"score": round(score, 1), "criterios": crit}


def motivos(pot: dict) -> list[dict]:
    """Frases para o "Por quê": só o que acrescenta (a favor, contra ou falta de dado)."""
    return [{"texto": c["texto"], "sinal": c["sinal"]} for c in pot["criterios"] if c["texto"]]
