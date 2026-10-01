# Plataforma Cross Sell · Innoa

Ferramenta que unifica os clientes das quatro verticais da Innoa, mede o nível de
relacionamento com cada empresa e pessoa a partir dos e-mails, e aponta **onde
existe espaço para cross sell, com quem falar e quem da Innoa deve fazer a ponte**.

| Vertical | Equipe | Fonte | Como entra |
|---|---|---|---|
| Linhas Financeiras | Victor Boldrini (líder), Pedro Acciari, Pamela Silva | Pipedrive — pipelines 1 (D&O/Cyber) e 40 (Garantia) | API (sincronização incremental) |
| Ramos Elementares | Bruno Rodrigues (líder) | Pipedrive — pipeline 29 | API |
| Saúde (Pipo) — novos negócios | Pedro Acciari, Pamela Silva | Pipedrive — pipelines 23, 34 e 38 | API |
| Saúde — renovações | | Zeca | Importação periódica de CSV/XLSX |
| Linhas Pessoais | a definir | Quiver | Importação periódica de CSV/XLSX |
| Relacionamento | | Microsoft 365 (Outlook) | Microsoft Graph, só metadados |
| Quem é a empresa / quem são as pessoas | | Receita Federal (BrasilAPI) + LinkedIn | API gratuita + exportação do Sales Navigator |

Equipes e pipelines ficam em `config/verticais.yaml`. Cada oportunidade mostra
**quem apresenta** (o usuário interno com mais relação com o cliente) e **quem atende**
(o líder da vertical-alvo; sem líder, a equipe). Quando quem tem a relação já atua na
vertical-alvo, por exemplo Pedro com um cliente de LF que ainda não tem Saúde, a
oportunidade indica que ele pode conduzir direto.

## Como funciona

```
Pipedrive ─┐
Zeca ──────┤                    ┌─ Score de relacionamento (pessoa e empresa)
Quiver ────┼─► Resolução de ────┤
E-mails ───┤   entidades        ├─ Pontos focais (até 3 por empresa)
Receita ───┤   (CNPJ, e-mail,   │
LinkedIn ──┘    CPF, nome)      └─ Oportunidades de cross sell ─► Painel / API
```

1. **Base unificada.** Tudo vira `Empresa`, `Pessoa` e `Negocio`. A mesma empresa
   vinda do Pipedrive e do Zeca é casada pelo CNPJ (campo customizado da organização
   no Pipedrive); se faltar CNPJ, usa domínio do site/e-mail e, por último, o nome
   normalizado (sem acento, sem "S.A.", "Ltda" etc.).
2. **Zeca e migrações.** Cada importação do Zeca é idempotente pelo número do
   contrato. Numa carga completa, contratos que estavam ativos e sumiram do arquivo
   são marcados como cancelados: é assim que a migração de operadora aparece.
3. **E-mails.** Lemos apenas remetente, destinatários, data e thread (sem assunto e
   sem corpo). Contatos novos em domínios de empresas conhecidas são criados
   automaticamente. Assim descobrimos pontos focais que não estão no CRM.
4. **Score de relacionamento (0–100).**
   - Pessoa: frequência nos últimos 90 dias (35%), recência (30%), reciprocidade,
     ou seja, threads com ida *e* volta (25%), e quantos usuários da Innoa falam com
     ela (10%).
   - Empresa: contato mais forte (40%), nº de contatos ativos (20%), acesso a um
     decisor (sócio/C-level/diretor) (20%) e presença multi-vertical (20%).
5. **Quem é cliente.** Só conta como cliente quem tem negócio **ganho** (Pipedrive) ou
   apólice **ativa** (Zeca/Quiver) **dentro da vigência**. A vigência vem dos campos
   customizados "início/fim de vigência" do negócio no Pipedrive. Negócio aberto no funil
   é prospecção. Negócio "ganho" que é cancelamento (título com "cancelamento" ou valor
   negativo) não conta. Vigência vencida marca a empresa como ex-cliente, e isso vira
   motivo de **reconquista**.
6. **Oportunidades.** Para cada cliente, cada vertical em que ele ainda não é cliente
   e não tem negócio aberto:
   `score = 45% relacionamento + 30% aderência + 25% momento`
   - *Aderência*: porte, nº de funcionários, capital social e CNAE (ex.: saúde para
     ≥30 funcionários; D&O/garantia para empresas maiores; RE para indústria e logística).
   - *Momento*: uma renovação em 30–120 dias em qualquer vertical é o melhor gancho.
   - *Linhas Pessoais* é por pessoa: sócios e executivos de clientes corporativos
     sem apólice PF. No sentido inverso, um cliente PF do Quiver que é sócio (QSA) de
     uma empresa gera oportunidade corporativa nessa empresa.
   - Cada oportunidade traz os **motivos** e a **ponte**: o usuário interno que mais
     conversa com aquela empresa/pessoa.
   - O status (`nova`, `em_andamento`, `convertida`, `descartada`) sobrevive aos recálculos.

## Sobre o LinkedIn

O LinkedIn não tem API aberta de Sales Navigator. A integração oficial (SNAP) é
restrita a parceiros como Salesforce, Dynamics, HubSpot, Outreach e Gong. Não existe
sync nativo com o Pipedrive, e o Sales Navigator não exporta listas de leads.
Ferramentas que raspam o LinkedIn violam os termos de uso e arriscam a conta.
O que dá para fazer de forma legítima:

- **Receita Federal (já implementado):** porte, CNAE, capital social e **quadro
  societário**. É gratuito e já traz os decisores.
- **Plataforma → Sales Navigator:** exportar as contas-alvo em CSV (nome, site) e
  subir como *Account List* no Sales Navigator (Advanced/Advanced Plus, até 1.000
  contas). O Sales Navigator passa a mostrar os decisores e os alertas dessas empresas.
- **Conexões de cada usuário:** cada pessoa da equipe pode baixar as próprias conexões
  (LinkedIn → Configurações → Privacidade de dados → Obter cópia dos dados → Conexões).
  O arquivo mostra quem da Innoa está conectado a quem no cliente.
- **Listas montadas manualmente** ou vindas de um provedor licenciado de dados B2B podem
  ser importadas pelo painel (`LinkedIn / Sales Navigator`).

## Instalação

```bash
pip install -e ".[dev]"
cp .env.example .env         # preencher tokens
# revisar config/verticais.yaml: pipeline -> vertical e usuários das verticais
crosssell initdb
```

## Uso

```bash
crosssell pipedrive                    # carga completa (depois: --dias 2 para incremental)
crosssell importar zeca exportacao_zeca.xlsx
crosssell importar quiver exportacao_quiver.csv
crosssell importar linkedin leads_sales_navigator.csv
crosssell enriquecer                   # Receita Federal
crosssell emails --dias 90             # Microsoft 365
crosssell recalcular
crosssell serve                        # painel em http://localhost:8000
```

Agendamento sugerido (cron): `pipedrive --dias 2` e `emails --dias 2` a cada hora,
`enriquecer` e `recalcular` diariamente, e importações do Zeca/Quiver sempre que
houver nova exportação (ou por uma pasta monitorada).

API JSON: `GET /api/oportunidades?vertical=saude&status=nova`, `GET /api/empresas/{id}`.

### Microsoft 365

Criar um *App registration* no Entra ID com permissão de **aplicativo** `Mail.Read`
e consentimento de administrador. Recomenda-se restringir o app às caixas dos
usuários das verticais com uma *Application Access Policy* do Exchange. Preencher
`MS_TENANT_ID`, `MS_CLIENT_ID` e `MS_CLIENT_SECRET`.

### LGPD

Só metadados de e-mail são armazenados, sem assunto nem corpo. A base legal sugerida
é o legítimo interesse na gestão do relacionamento com clientes; vale formalizar isso
com o DPO e informar os usuários das verticais.

## Prévia com dados de exemplo

```bash
DATABASE_URL=sqlite:///demo.db python scripts/demo.py               # empresas fictícias
DATABASE_URL=sqlite:///demo.db python scripts/exportar_preview.py preview.html --exemplo
```

`scripts/exportar_preview.py` gera um HTML único, sem servidor, a partir de qualquer
banco. Rodado sobre o banco real, sem `--exemplo`, produz um retrato navegável da base.

## Desenvolvimento

```bash
pytest
```

## Pendências para colocar em produção

- [ ] Confirmar os pipelines 38 (Flash Saúde), 31 (M&A) e 39 (Canais/parcerias).
- [ ] Definir líder de Saúde e equipe de Linhas Pessoais.
- [ ] Obter uma exportação real do **Zeca** e do **Quiver** para ajustar os
      cabeçalhos em `config/verticais.yaml`, e verificar se algum deles oferece API.
- [ ] Criar o app registration no Microsoft 365.
- [ ] Definir o provedor de dados de pessoas (Sales Navigator ou outro).
- [ ] Hospedagem (Postgres + container) e login (SSO Microsoft).
- [ ] Opcional: criar a atividade/negócio no Pipedrive direto a partir da oportunidade.
