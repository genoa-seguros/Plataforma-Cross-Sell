# Colocar no ar

> Na AWS da Innoa: siga [`deploy/LEIA-ME.md`](../deploy/LEIA-ME.md) (EC2 + RDS em São Paulo). Este
> resumo serve para outro provedor de container.

A plataforma é um único container (`Dockerfile`) + um banco Postgres + uma tarefa de hora em hora.

| Peça | O que roda |
|---|---|
| Web | `Dockerfile`: aplica as migrações do banco (`crosssell initdb`) e sobe o `uvicorn` na porta `$PORT` |
| Rotina | `crosssell rotina`, de hora em hora (Pipedrive, e-mails, notícias, Receita, LinkedIn, scores; qualidade 1×/semana). Com `ROTINA_INTERNA=true` ela roda dentro do próprio container web; sem isso, agende `crosssell rotina` no cron do provedor |
| Banco | Postgres (a URL `postgres://...` do provedor funciona direto em `DATABASE_URL`) |

## Variáveis de ambiente (segredos)

Mesmas do `deploy/env.exemplo`. As obrigatórias para começar:

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
crosssell pipedrive              # primeira carga completa (alguns minutos)
crosssell convidar rodrigo.pedroni@innoaseguros.com.br "Rodrigo Pedroni" --master --base-url https://<endereço-da-plataforma>
```

O último comando imprime um link de convite: envie ao master, que cria a própria senha nele (vale
7 dias). Depois, o master entra, abre **Equipe** e convida o time.

## Exigências

- **HTTPS** (os cookies de sessão são `Secure`): na AWS, o Caddy do `deploy/` emite o certificado;
  em outro provedor, use o HTTPS dele.
- Saída para a internet. A rotina chama `innoaseguros.pipedrive.com`, `api.linkedapi.io`,
  `graph.microsoft.com`, `login.microsoftonline.com`, `api.anthropic.com`, `news.google.com` e
  `brasilapi.com.br`, e também abre o site de cada empresa para achar o link do LinkedIn: uma lista
  fechada de endereços não basta.
- Backup diário do Postgres (os provedores gerenciados fazem).
