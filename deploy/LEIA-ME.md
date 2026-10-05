# Instalação na AWS (região São Paulo, sa-east-1)

Arquitetura: **1 servidor EC2** (Docker: plataforma + Caddy para HTTPS automático) e **1 banco RDS
PostgreSQL**. A rotina de hora em hora (Pipedrive, e-mails, LinkedIn, notícias, Receita, qualidade)
roda dentro do container (`ROTINA_INTERNA=true`).

## 1. Banco (RDS PostgreSQL)

- Engine PostgreSQL 16, classe `db.t4g.micro`, 20 GB gp3, **sem acesso público**.
- Nome do banco `crosssell`, usuário `crosssell`, senha forte.
- Backups automáticos: 7 dias. Criptografia em repouso: ligada.
- Security group do banco: porta **5432** liberada **somente** para o security group do servidor EC2.

## 2. Servidor (EC2)

- Ubuntu 24.04 LTS, `t3.small` (2 GB RAM), disco 20 GB gp3, **Elastic IP**.
- Security group: **80 e 443** abertos para a internet; **22** só do IP da TI.
- Saída para a internet liberada (Pipedrive, Linked API, Microsoft Graph, Anthropic, Google Notícias, BrasilAPI).

## 3. Código (GitHub, leitura)

No servidor:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/crosssell -N "" && cat ~/.ssh/crosssell.pub
```

Cadastre a chave pública em **GitHub → genoaseguros/Plataforma-Cross-Sell → Settings → Deploy keys →
Add deploy key** (sem "Allow write access"). Depois:

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/crosssell" git clone -b feat/plataforma-cross-sell-mvp \
  git@github.com:genoaseguros/Plataforma-Cross-Sell.git crosssell
cd crosssell && git config core.sshCommand "ssh -i ~/.ssh/crosssell"
```

## 4. Configuração e subida

```bash
cp deploy/env.exemplo deploy/.env && nano deploy/.env   # preencher (ver comentários)
bash deploy/instalar.sh
```

`DATABASE_URL` = `postgres://crosssell:SENHA@ENDPOINT-DO-RDS:5432/crosssell`.

## 5. DNS

Registro **A** `crosssell.innoaseguros.com.br` → Elastic IP do servidor. O Caddy emite o certificado
HTTPS sozinho em 1–2 minutos depois que o DNS propagar.

## 6. Primeira carga e acesso do master

```bash
cd ~/crosssell/deploy
sudo docker compose exec web crosssell pipedrive          # carga completa (alguns minutos)
sudo docker compose exec web crosssell convidar rodrigo.pedroni@innoaseguros.com.br "Rodrigo Pedroni" \
  --master --base-url https://crosssell.innoaseguros.com.br
```

O último comando imprime um link: envie ao Rodrigo; ele cria a senha nele (vale 7 dias).

## 7. Microsoft 365 (leitura de e-mails e "esqueci minha senha")

1. **Entra ID → App registrations → New registration**: "Innoa Cross Sell", single tenant.
2. **API permissions → Microsoft Graph → Application permissions**: `Mail.Read` e `Mail.Send` →
   **Grant admin consent**.
3. **Certificates & secrets → New client secret** (24 meses). Anote *Tenant ID*, *Client ID* e o *secret*.
4. Caixa remetente: crie a caixa compartilhada `crosssell@innoaseguros.com.br`.
5. Restrinja o app às caixas da equipe (obrigatório; sem isso o app enxerga todas as caixas):
   crie o grupo de segurança habilitado para e-mail `crosssell-equipe@innoaseguros.com.br` com as
   pessoas que usarão a plataforma + a caixa `crosssell@`, e no Exchange Online PowerShell:
   ```powershell
   New-ApplicationAccessPolicy -AppId <CLIENT_ID> -PolicyScopeGroupId crosssell-equipe@innoaseguros.com.br `
     -AccessRight RestrictAccess -Description "Innoa Cross Sell"
   Test-ApplicationAccessPolicy -Identity rodrigo.pedroni@innoaseguros.com.br -AppId <CLIENT_ID>
   ```
6. Preencha `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` e `EMAIL_REMETENTE` em `deploy/.env` e
   rode `sudo docker compose up -d web`.

## 8. Claude API (temperatura dos e-mails)

Em console.anthropic.com → API Keys, crie uma chave para a Innoa e coloque em `ANTHROPIC_API_KEY`.

## Operação

- Atualizar para a versão mais nova: `bash deploy/atualizar.sh`
- Logs: `cd deploy && sudo docker compose logs -f web`
- Rodar a rotina na hora: `sudo docker compose exec web crosssell rotina`
- Backup: automático no RDS (7 dias). Os segredos ficam só em `deploy/.env` no servidor.
