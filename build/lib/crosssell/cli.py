from datetime import datetime, timedelta
from pathlib import Path

import typer

from crosssell.config import get_settings
from crosssell.db import SessionLocal, init_db

app = typer.Typer(help="Plataforma de Cross Sell Innoa")


def _db():
    init_db()
    return SessionLocal()


@app.command()
def initdb():
    """Cria as tabelas e a equipe inicial do config (sem senha; cada um entra por convite)."""
    from crosssell.pipeline import carregar_usuarios

    typer.echo(f"{carregar_usuarios(_db(), get_settings())} usuários criados")


@app.command("criar-master")
def criar_master(email: str, nome: str):
    """Cria (ou promove) o usuário master e define a senha dele."""
    from crosssell import auth

    db = _db()
    senha = typer.prompt("Senha", hide_input=True, confirmation_prompt=True)
    erro = auth.validar_senha(senha)
    if erro:
        raise typer.BadParameter(erro)
    u, _ = auth.convidar(db, email, nome, verticais=[], papel="master")
    u.papel = "master"
    auth.aceitar_convite(db, u, senha)
    typer.echo(f"Master {u.email} pronto.")


@app.command()
def convidar(email: str, nome: str, base_url: str = typer.Option("http://localhost:8000"),
             vertical: list[str] = typer.Option([], help="linhas_financeiras | saude | ramos_elementares")):
    """Gera um link de convite (o mesmo que a tela Equipe gera)."""
    from crosssell import auth

    _, token = auth.convidar(_db(), email, nome, vertical)
    typer.echo(f"{base_url.rstrip('/')}/convite/{token}")


@app.command()
def pipedrive(dias: int = typer.Option(0, help="Somente alterados nos últimos N dias (0 = tudo)")):
    """Sincroniza organizações, pessoas, negócios e o status das atividades do Pipedrive."""
    from datetime import date

    from crosssell.connectors import pipedrive as pd
    from crosssell.pipeline import registrar

    db, s = _db(), get_settings()
    client = pd.cliente(s)
    desde = datetime.utcnow() - timedelta(days=dias) if dias else None
    typer.echo(registrar(db, "pipedrive", pd.sincronizar, db, s, client, updated_since=desde))
    typer.echo(f"{pd.sincronizar_atividades(db, client, date.today() - timedelta(days=30))} atividades atualizadas")


@app.command()
def importar(fonte: str = typer.Argument(..., help="zeca | linkedin"), arquivo: Path = typer.Argument(...),
             parcial: bool = typer.Option(False, help="Zeca: não cancelar apólices ausentes do arquivo")):
    """Importa uma exportação (CSV/XLSX) do Zeca ou uma lista de pessoas (LinkedIn)."""
    from crosssell.connectors import enriquecimento, planilhas
    from crosssell.pipeline import registrar

    db, s = _db(), get_settings()
    conteudo = arquivo.read_bytes()
    if fonte == "zeca":
        res = registrar(db, "zeca", planilhas.importar_zeca, db, s, conteudo, arquivo.name, carga_completa=not parcial)
    elif fonte == "linkedin":
        res = registrar(db, "linkedin", enriquecimento.importar_linkedin, db, s, conteudo, arquivo.name)
    else:
        raise typer.BadParameter("fonte deve ser zeca ou linkedin")
    typer.echo(res)


@app.command()
def emails(dias: int = 30, temperatura: bool = typer.Option(True, help="Classificar a temperatura com a Claude API")):
    """Lê os e-mails (Microsoft 365) dos usuários ativos e atualiza a temperatura dos contatos."""
    from crosssell.connectors import email_m365
    from crosssell.pipeline import registrar
    from crosssell.temperatura import Classificador

    db, s = _db(), get_settings()
    classificador = Classificador(s) if temperatura else None
    typer.echo(registrar(db, "email", email_m365.sincronizar, db, s, dias=dias, classificador=classificador))


@app.command()
def noticias(horas: int = typer.Option(24, help="Não rebuscar empresas atualizadas há menos de N horas")):
    """Busca notícias das empresas que estão na tabela de negócios abertos."""
    from crosssell import tabela
    from crosssell.connectors import noticias as nt
    from crosssell.pipeline import registrar

    db, s = _db(), get_settings()
    ids = sorted({x["empresa"]["id"] for x in tabela.montar(db, s) if x["empresa"]})
    typer.echo(registrar(db, "noticias", nt.atualizar, db, ids, horas=horas))


@app.command("linkedin")
def linkedin():
    """LinkedIn: aplica os resultados prontos e inicia as próximas consultas (Linked API direto;
    sem os tokens dela, dispara o n8n)."""
    from crosssell.connectors import linkedin as lk
    from crosssell.pipeline import registrar

    db, s = _db(), get_settings()
    if lk.direto(s):
        typer.echo(registrar(db, "linkedin", lk.executar, db, s))
    else:
        typer.echo(registrar(db, "linkedin-disparo", lk.disparar, db, s))


@app.command("linkedin-sites")
def linkedin_sites(limite: int = 50):
    """Procura o link do LinkedIn no site de cada empresa da tabela (sem usar a Linked API)."""
    from crosssell.connectors import linkedin as lk
    from crosssell.pipeline import registrar

    db = _db()
    typer.echo(registrar(db, "linkedin-sites", lk.descobrir_por_site, db, get_settings(), limite=limite))


@app.command()
def qualidade():
    """Revisão do cadastro: organizações duplicadas e razão social (resultado na aba Qualidade)."""
    from crosssell import qualidade as q
    from crosssell.pipeline import registrar

    db = _db()
    typer.echo(registrar(db, "qualidade", q.resumo, db))


def _ultima_ha_dias(fonte: str) -> float:
    from sqlalchemy import select

    from crosssell.models import SyncLog

    db = _db()
    ultima = db.scalar(select(SyncLog.inicio).where(SyncLog.fonte == fonte, SyncLog.erro.is_(None))
                       .order_by(SyncLog.inicio.desc()))
    return 999 if ultima is None else (datetime.utcnow() - ultima).total_seconds() / 86400


@app.command()
def rotina(dias: int = 2):
    """Roda tudo em sequência (para o agendador): Pipedrive, e-mails, notícias, LinkedIn e scores."""
    from crosssell.connectors import linkedin as lk

    passos = [("pipedrive", lambda: pipedrive(dias=dias)), ("emails", lambda: emails(dias=dias, temperatura=True)),
              ("noticias", lambda: noticias(horas=24)), ("receita", lambda: enriquecer(limite=50)),
              ("linkedin-sites", linkedin_sites), ("recalcular", recalcular)]
    if lk.configurado(get_settings()):
        passos.insert(5, ("linkedin", linkedin))
    if _ultima_ha_dias("qualidade") >= 7:  # revisão do cadastro: semanal
        passos.append(("qualidade", qualidade))
    for nome, fn in passos:
        try:
            fn()
        except Exception as exc:  # um passo com falha não impede os outros; o erro fica no log de sincronização
            typer.echo(f"{nome}: falhou ({exc})", err=True)


@app.command()
def enriquecer(limite: int = 200):
    """Enriquece empresas com dados da Receita (porte, CNAE, sócios)."""
    from crosssell.connectors import enriquecimento
    from crosssell.pipeline import registrar

    db = _db()
    typer.echo(registrar(db, "receita", enriquecimento.enriquecer_todas, db, limite=limite))


@app.command()
def recalcular():
    """Recalcula os scores de relacionamento."""
    from crosssell.pipeline import recalcular as rc

    typer.echo(rc(_db()))


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000):
    """Sobe a plataforma web."""
    import uvicorn

    init_db()
    uvicorn.run("crosssell.web.app:app", host=host, port=port)


if __name__ == "__main__":
    app()
