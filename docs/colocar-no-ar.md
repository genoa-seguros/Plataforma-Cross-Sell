# Colocar no ar

A plataforma é um único container (`Dockerfile`) + um banco Postgres + uma tarefa de hora em hora.

| Peça | O que roda |
|---|---|
| Web | `Dockerfile` (cria as tabelas e sobe `uvicorn` na porta `$PORT`) |
| Rotina | `crosssell rotina`, de hora em hora (Pipedrive, e-mails, notícias, Receita, LinkedIn, scores; qualidade 1×/semana) |
| Banco | Postgres (a URL `postgres://...` do provedor funciona direto em `DATABASE_URL`) |

## Variáveis de ambiente (segredos)

Mesmas do `.env.example`. As obrigatórias para começar:

```
DATABASE_URL=postgres://...
COOKIE_SEGURO=true
PIPEDRIVE_API_TOKEN=...          PIPEDRIVE_COMPANY_DOMAIN=innoaseguros
LINKED_API_TOKEN=...             LINKED_API_IDENTIFICATION_TOKEN=...
```

Depois, quando o Microsoft 365 estiver autorizado: `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`,
`EMAIL_REMETENTE`, e a chave `ANTHROPIC_API_KEY` (temperatura dos e-mails).

## Primeiro acesso

No console do serviço web (uma vez):

```
crosssell criar-master rodrigo.pedroni@innoaseguros.com.br "Rodrigo Pedroni"
crosssell pipedrive --dias 3650   # primeira carga completa
```

Depois, o master entra, abre **Equipe** e convida o time.

## Exigências

- **HTTPS** (cookies seguros). Os provedores abaixo já entregam.
- A rede do servidor precisa alcançar: `innoaseguros.pipedrive.com`, `api.linkedapi.io`,
  `graph.microsoft.com`, `login.microsoftonline.com`, `api.anthropic.com`, `news.google.com`,
  `brasilapi.com.br`.
- Backup diário do Postgres (os provedores gerenciados fazem).
