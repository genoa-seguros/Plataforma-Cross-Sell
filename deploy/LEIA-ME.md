# Instalação na AWS (EC2 `zeca-server`, us-east-1)

Arquitetura do MVP: a plataforma roda na EC2 **`zeca-server`**, ao lado da API do Zeca, sem custo
extra de servidor nem de banco.

| Peça | Como roda |
|---|---|
| Plataforma | Container `web`, só em `127.0.0.1:8000` (a porta 3333 é da API do Zeca). Limite de 512 MB: se passar, só ela reinicia. |
| Banco | Postgres 16 no container `postgres`, sem porta publicada (só a plataforma o enxerga). Dados num volume do Docker. |
| Backup | Container `backup`: uma cópia do banco por dia em `/opt/crosssell-backups`, guardando 7 dias. Fica no disco do servidor (sem cópia para fora por enquanto). |
| HTTPS | O Nginx que já roda no servidor, com certificado do Certbot (igual ao `api.zeca.coinsure.com.br`). |
| Rotina | De hora em hora, dentro do container `web` (`ROTINA_INTERNA=true`). |

Endereço: **https://crosssell.coinsure.com.br**. Código em **`/opt/crosssell`** (fora de
`/home/ubuntu`; nunca dentro de `actions-runner/_work`, que o runner do GitHub apaga a cada deploy do
Zeca).

## 1. DNS (Route 53)

Registro **A** `crosssell.coinsure.com.br` → `100.25.253.31` (Elastic IP da `zeca-server`), na zona
`coinsure.com.br`:

```bash
aws route53 change-resource-record-sets --hosted-zone-id Z102748431W69TB2FEDG3 --change-batch '{"Changes":[{"Action":"CREATE","ResourceRecordSet":{"Name":"crosssell.coinsure.com.br","Type":"A","TTL":300,"ResourceRecords":[{"Value":"100.25.253.31"}]}}]}'
```

## 2. Código (GitHub, leitura)

Na `zeca-server` (`ssh -i zeca-server.pem ubuntu@100.25.253.31`):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/crosssell -N "" && cat ~/.ssh/crosssell.pub
```

Cadastre a chave pública em **GitHub → genoaseguros/Plataforma-Cross-Sell → Settings → Deploy keys →
Add deploy key** (sem "Allow write access"). Depois:

```bash
sudo mkdir -p /opt/crosssell && sudo chown ubuntu:ubuntu /opt/crosssell
GIT_SSH_COMMAND="ssh -i ~/.ssh/crosssell" git clone -b main \
  git@github.com:genoaseguros/Plataforma-Cross-Sell.git /opt/crosssell
cd /opt/crosssell && git config core.sshCommand "ssh -i ~/.ssh/crosssell"
```

## 3. Configuração

```bash
cd /opt/crosssell
cp deploy/env.exemplo deploy/.env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" deploy/.env
nano deploy/.env   # tokens do Pipedrive e da Linked API (os do Microsoft 365 e da Claude podem vir depois)
```

## 4. Subida

```bash
bash deploy/instalar.sh
```

Constrói a imagem e sobe os três containers. Na primeira subida, o container `web` cria as tabelas
(migrações). Confira: `curl -sI http://127.0.0.1:8000/login` deve responder `200`.

## 5. Nginx e certificado HTTPS

Depois que o DNS do passo 1 responder (`dig +short crosssell.coinsure.com.br` → `100.25.253.31`):

```bash
sudo cp /opt/crosssell/deploy/nginx-crosssell.conf /etc/nginx/sites-available/crosssell
sudo ln -s /etc/nginx/sites-available/crosssell /etc/nginx/sites-enabled/crosssell
sudo nginx -t && sudo systemctl reload nginx     # nginx -t confere antes de recarregar (a API do Zeca usa o mesmo Nginx)
sudo certbot --nginx -d crosssell.coinsure.com.br
```

O Certbot emite o certificado, acrescenta o HTTPS no arquivo do site e o renova sozinho (o
`certbot.timer` do servidor já faz isso para a API do Zeca).

## 6. Primeira carga e acesso do master

```bash
cd /opt/crosssell/deploy
docker compose exec web crosssell pipedrive          # carga completa (alguns minutos)
docker compose exec web crosssell convidar rodrigo.pedroni@innoaseguros.com.br "Rodrigo Pedroni" \
  --master --base-url https://crosssell.coinsure.com.br
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
   rode `docker compose up -d web` (na pasta `deploy`).

## 8. Claude API (temperatura dos e-mails)

Em console.anthropic.com → API Keys, crie uma chave para a Innoa e coloque em `ANTHROPIC_API_KEY`;
depois `docker compose up -d web`.

## Operação

Comandos na pasta `/opt/crosssell/deploy`:

- **Atualizar** para a versão mais nova: `bash atualizar.sh` (o container aplica as migrações do banco ao subir).
- **Logs:** `docker compose logs -f web` (backup: `docker compose logs backup`).
- **Rotina na hora:** `docker compose exec web crosssell rotina` (se a rotina automática estiver rodando,
  esta é pulada).
- **Memória e CPU:** `docker stats --no-stream`.
- **Backups:** `ls -lh /opt/crosssell-backups`. Restaurar um deles (substitui o banco atual):
  ```bash
  docker compose stop web
  gunzip -c /opt/crosssell-backups/crosssell-AAAA-MM-DD.sql.gz | docker compose exec -T postgres sh -c \
    'dropdb -U crosssell --force crosssell && createdb -U crosssell crosssell && psql -q -v ON_ERROR_STOP=1 -U crosssell crosssell > /dev/null'
  docker compose start web
  ```
- Os segredos ficam só em `deploy/.env` no servidor.
