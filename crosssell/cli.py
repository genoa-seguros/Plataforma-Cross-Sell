from datetime import datetime, timedelta
from pathlib import Path

import typer
from sqlalchemy import select

from crosssell.config import get_settings
from crosssell.db import SessionLocal, init_db
from crosssell.models import UsuarioInterno

app = typer.Typer(help="Plataforma de Cross Sell Innoa")


def _db():
    init_db()
    return SessionLocal()


@app.command()
def initdb():
    """Cria as tabelas e carrega os usuários internos do config."""
    db = _db()
    for u in get_settings().verticais_config().get("usuarios", []):
        if not db.scalar(select(UsuarioInterno).where(UsuarioInterno.email == u["email"].lower())):
            db.add(UsuarioInterno(email=u["email"].lower(), nome=u["nome"], vertical=u["vertical"]))
    db.commit()
    typer.echo("ok")


@app.command()
def pipedrive(dias: int = typer.Option(0, help="Somente alterados nos últimos N dias (0 = tudo)")):
    """Sincroniza organizações, pessoas e negócios do Pipedrive."""
    from crosssell.connectors import pipedrive as pd
    from crosssell.pipeline import registrar

    db = _db()
    desde = datetime.utcnow() - timedelta(days=dias) if dias else None
    typer.echo(registrar(db, "pipedrive", pd.sincronizar, db, get_settings(), updated_since=desde))


@app.command()
def importar(fonte: str = typer.Argument(..., help="zeca | quiver | linkedin"), arquivo: Path = typer.Argument(...),
             parcial: bool = typer.Option(False, help="Zeca: não cancelar apólices ausentes do arquivo")):
    """Importa uma exportação (CSV/XLSX) do Zeca, Quiver ou LinkedIn/Sales Navigator."""
    from crosssell.connectors import enriquecimento, planilhas
    from crosssell.pipeline import registrar

    db, s = _db(), get_settings()
    conteudo = arquivo.read_bytes()
    if fonte == "zeca":
        res = registrar(db, "zeca", planilhas.importar_zeca, db, s, conteudo, arquivo.name, carga_completa=not parcial)
    elif fonte == "quiver":
        res = registrar(db, "quiver", planilhas.importar_quiver, db, s, conteudo, arquivo.name)
    elif fonte == "linkedin":
        res = registrar(db, "linkedin", enriquecimento.importar_linkedin, db, s, conteudo, arquivo.name)
    else:
        raise typer.BadParameter("fonte deve ser zeca, quiver ou linkedin")
    typer.echo(res)


@app.command()
def emails(dias: int = 30):
    """Lê metadados de e-mail (Microsoft 365) dos usuários das verticais."""
    from crosssell.connectors import email_m365
    from crosssell.pipeline import registrar

    db = _db()
    typer.echo(registrar(db, "email", email_m365.sincronizar, db, get_settings(), dias=dias))


@app.command()
def enriquecer(limite: int = 200):
    """Enriquece empresas com dados da Receita (porte, CNAE, sócios)."""
    from crosssell.connectors import enriquecimento
    from crosssell.pipeline import registrar

    db = _db()
    typer.echo(registrar(db, "receita", enriquecimento.enriquecer_todas, db, limite=limite))


@app.command()
def recalcular():
    """Recalcula scores de relacionamento e oportunidades."""
    from crosssell.pipeline import recalcular as rc

    typer.echo(rc(_db()))


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000):
    """Sobe o painel web."""
    import uvicorn

    init_db()
    uvicorn.run("crosssell.web.app:app", host=host, port=port)


if __name__ == "__main__":
    app()
