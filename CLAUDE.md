# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Innoa's cross-sell platform (Python 3.11, FastAPI + SQLAlchemy 2 + Typer). It shows clients/leads of one
vertical (`linhas_financeiras`, `saude`, `ramos_elementares`) that don't yet have or negotiate another
vertical, ranked by "Potencial". The code, identifiers, comments, UI, commit messages and docs are all in
**Brazilian Portuguese**. Keep that convention. The README holds the business rules (what counts as
"seguro vigente", how Potencial and Score de Influência are computed). Read it before changing scoring
or table logic.

## Commands

```bash
pip install -e ".[dev]"                      # installs the `crosssell` CLI (crosssell/cli.py)
pytest                                       # all tests (tests/)
pytest tests/test_fluxo.py::test_regra_de_vigencia   # single test
crosssell initdb                             # apply DB migrations only; users are created by a master on the Equipe screen
crosssell serve                              # web app at http://localhost:8000 (set COOKIE_SEGURO=false locally)
crosssell rotina                             # hourly job: pipedrive, pipedrive-excluidas, cnpj-sites, dominios, emails, noticias, receita, linkedin-sites, sites-ia (SITE_IA_ATIVO), linkedin, recalcular, qualidade (weekly)
crosssell site-teste 123 "Nome"              # test the site AI reading on chosen companies (--aplicar saves)
DATABASE_URL=sqlite:///demo.db python scripts/demo.py                         # fake demo data
DATABASE_URL=sqlite:///demo.db python scripts/exportar_preview.py preview.html --exemplo   # static single-file preview
```

There is no linter or formatter configured.

## Architecture

**Data flow:** connectors (`crosssell/connectors/`) pull from external sources → `resolver.py` matches
records to the same `Empresa`/`Pessoa` across sources (company: CNPJ > Pipedrive id > domain > normalized
name; person: CPF > e-mail > Pipedrive id > name+company; `_preencher` only fills empty fields, never
overwrites) → `models.py` (SQLAlchemy) → scoring → `tabela.py` builds the JSON the UI renders.

- **Every CLI sync goes through `pipeline.registrar(db, fonte, fn, ...)`**, which writes a `SyncLog` row
  (start/end/error). Connector functions return a `dict` whose int values are summed as the record count.
  `rotina` runs each step independently. One failing step does not stop the others.
- **Pipedrive** (`connectors/pipedrive.py`, API v2, incremental) is the main source. Funnel → vertical
  mapping, whether a funnel's open deals enter the table (`tabela: true`), and custom-field hashes live in
  `config/verticais.yaml`, read through `Settings` helpers (`pipelines()`, `campos_pipedrive()`). The
  platform also *writes* to Pipedrive: activities, org merges and razão social updates (`qualidade.py`).
- **"Vigente" logic lives on `Negocio` properties** (`vigente`, `saude_vitalicio`, `saude_desmarcada`,
  `ex_cliente`) in `models.py`. Saúde has no end date, and a `fonte="manual"` cancelled Saúde deal is how
  the UI "unchecks" a Saúde client.
- **Scoring:** `potencial.py` computes Potencial per vertical from weights/keyword lists in
  `config/criterios.yaml` (reloaded by file mtime, no restart needed locally; in production the file is
  baked into the Docker image, so a change needs a commit plus `deploy/atualizar.sh`) plus `influencia()` (50% e-mail
  relationship from `scoring/relacionamento.py`, 50% job-title hierarchy). `temperatura.py` uses the Claude
  API to classify contact openness from e-mail replies. E-mail text is never stored. The model is the one a master
  picked on the Equipe screen (`configuracoes` table, `temperatura.modelo_em_uso`), else `ANTHROPIC_MODEL`; only
  models in `temperatura.MODELOS` are offered (they must accept effort, structured output and the server fallback).
  `site_ia.py` uses the same model and call pattern to read a company's site for LF (fundos/investidores, grandes
  clientes, serviço intelectual, site profissional) into `Empresa.site_ia`; the site text is never stored. Off until
  `SITE_IA_ATIVO=true`.
- **LinkedIn** (`connectors/linkedin.py`, `linkedapi.py`): calls Linked API directly when
  `LINKED_API_TOKEN`/`LINKED_API_IDENTIFICATION_TOKEN` are set. Otherwise it falls back to an n8n webhook
  (`n8n/`, `docs/linkedin-n8n.md`). Targets come from `linkedin.alvos()` in Score order, each tagged with the
  vertical that pays (the company's highest-Score opportunity) and a quota group; `escolher()` applies the daily
  per-vertical quotas (`LINKEDIN_COTA_*`, leftover passes to others). Saúde companies follow `_passos_saude`
  (page → praça via Sales Navigator employee locations → decision makers by cargo; SN actions only with
  `LINKEDIN_SALES_NAVIGATOR=true`, test first with `crosssell linkedin-teste`). LF companies follow `_passos_lf` (page
  only if VC/setor/funcionários/urn is missing → SN list of `linhas_financeiras.linkedin_cargos`, stored under
  `linkedin_areas["pessoas_lf"]`, re-read early after an M&A news item); LF quota split by `grupo_lf` (Pipo funnel or not). Rate-limited by
  `LINKEDIN_LOTE`/`LINKEDIN_LIMITE_DIA` and tracked in
  `LinkedinPedido` rows.
- **Web** (`crosssell/web/app.py`): server-rendered Jinja templates for auth pages only. The main UI is a
  single static file, `web/app.html`, that calls `/api/*` with `fetch`. The same HTML is reused by
  `scripts/exportar_preview.py`, which injects data at the `/*__DADOS__*/null` placeholder (`const D`), so
  UI changes must keep working in both modes. Auth uses scrypt passwords and hashed session tokens in
  cookies. Non-GET API calls must send the `X-Cross-Sell: 1` header (CSRF guard in `usuario_atual`), and
  master-only routes use `somente_master`; Qualidade routes use `acesso_qualidade` (master or `papel="head"`).
  `ROTINA_INTERNA=true` makes the server run `crosssell rotina` in a background thread.
- **Oportunidades shows only "analisadas"** (`tabela.VERTICAIS_OPORTUNIDADES`: `saude` and `linhas_financeiras`;
  `tabela.faltando()` says what is missing. Saúde: praça/cidade, funcionários, setor. LF (`faltando_lf`): Receita,
  notícias, site (IA), cargos-chave. Both: quem decide, ponte por e-mail. `tabela.FINAIS` ("fora da praça", "MEI") never
  enter, not even the queue). LF is per product (`potencial.FITS`: E&O, D&O, Cyber, IMI; `tabela.produtos_da_empresa`):
  one row per company with suggested products, `tipo` "cross" (default view) or "mais_lf" (`?oferta=mais_lf`), and
  `etiquetas` from open and lost (`Negocio.perdido_em`) LF deals. LF Score: profissionalização, momento (news from
  `noticias.PORTAIS` weigh more), produto, influência. Employees and sector count only from LinkedIn or manual
  (`potencial.funcionarios_validos` / `setor_valido`); Pipedrive's are ignored and CNAE is not a sector. The ponte is
  e-mail only: `tabela.com_relacao` (wrote to and got a reply from the team in the last 12 months). A newly enabled
  mailbox reads 12 months once (`Usuario.historico_em`). `potencial.praca(e)` decides the Saúde praça: a manual city
  (`cidade_fonte="manual"`) decides alone, a city in `metropoles` is alvo, otherwise `Empresa.praca` (set by the
  employee-location reading). `?empresa=` returns every opportunity, analyzed or not, for the ficha.
- **Mailbox reading** is opt-in per user: `Usuario.le_emails` (off by default, toggled by a master on the Equipe
  screen, only for users who have logged in) AND membership in the Exchange Application Access Policy group
  `crosssell-equipe`. `email_m365.sincronizar` records `leitura_em` / `leitura_erro` ("recusada" = 403).
- **Config:** `config.py` `Settings` (pydantic-settings, `.env`), cached by `get_settings()` (lru_cache).
  `db.py` creates the engine at import time from `DATABASE_URL` (SQLite locally, Postgres in prod;
  `postgres://` URLs are rewritten to `postgresql+psycopg://`).
- **Migrations (Alembic):** `crosssell/migrations/` lives inside the package so it ships in the Docker image.
  `db.init_db()` runs `alembic upgrade head` (under a Postgres advisory lock) and is called by every CLI
  command and at server startup; a database created by the old `create_all` is stamped `0001` first. After
  changing `models.py`, run `alembic revision --autogenerate -m "..."` (uses `DATABASE_URL` from `.env`) and
  commit the reviewed file. `tests/test_migracoes.py` fails when models and migrations diverge. Tests still
  build their in-memory DB with `create_all`.
- **Experiments that touch `models.py` or migrations:** do them in a copy or worktree, not in the working
  directory. The user often keeps `uvicorn --reload` running against `demo.db`, and a reload runs `init_db()`,
  applying whatever migration is on disk.

## Tests

`tests/conftest.py` provides an in-memory SQLite `engine`/`db` and a `settings` fixture built from the real
`config/verticais.yaml` with no `.env`. External APIs are faked with `httpx.MockTransport`.
`tests/fakes.py::FakePipedrive` serves fixed orgs/persons/deals and records writes (`criadas`,
`atualizadas`). `tests/test_fluxo.py::carregar` loads that fake into the DB. Web tests (`test_web.py`)
override `get_db`/`get_pipedrive` via `app.dependency_overrides` and monkeypatch `get_settings`. Inject
clients the same way (connectors accept a `transport`/client) rather than hitting real APIs.

## Gotchas

- `build/` is pip/setuptools output and is gitignored. Edit `crosssell/`, never `build/`.
- Deployment: `Dockerfile` (runs `initdb` then uvicorn) and `deploy/` (MVP on the existing EC2 `zeca-server`,
  next to the Zeca API: `web` + `postgres` + daily local `backup` containers in `/opt/crosssell`, behind the host
  Nginx with Certbot at crosssell.coinsure.com.br; see `deploy/LEIA-ME.md`).
- CI/CD: `.github/workflows/testes.yml` runs pytest on every PR; `deploy.yml` runs it again on each push to
  `main` and then deploys through AWS SSM (OIDC role `github-crosssell-deploy`, SSM document `CrossSell-Atualizar`,
  which only runs `deploy/atualizar.sh`). Files for the one-time AWS setup are in `deploy/aws/`.
