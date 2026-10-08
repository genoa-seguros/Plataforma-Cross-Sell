# Plataforma Cross Sell · Innoa

Ferramenta tática para a reunião semanal (sexta-feira) das verticais **Linhas Financeiras, Saúde e
Ramos Elementares**. Mostra o **cross sell que ninguém está trabalhando**: clientes e leads de uma
vertical que ainda não têm nem negociam outra, ordenados pelo potencial, com quem decide e quem
pode apresentar. A partir dela a equipe cria atividades no Pipedrive e acompanha os to-dos da semana.

## Telas

- **Login**: e-mail e senha, com *Esqueci minha senha* (link de uso único por e-mail, vale 1 hora;
  o master também gera esse link na tela Equipe).
- **Oportunidades** (tela inicial): o cross sell que **ninguém está trabalhando**. Clientes
  (seguro vigente) e leads com **algum card aberto** no Pipedrive (negócio aberto com vertical, em
  qualquer funil) que ainda não têm seguro nem negócio aberto em outra vertical (ex.: tem D&O e está
  renovando, não tem Saúde), ordenados pelo **Potencial** na vertical da oportunidade. Quem não tem
  nada aberto fica de fora (terá uma tela própria, ainda a desenhar). Só aparecem as empresas já
  **analisadas**: em Saúde, praça decidida (cidade alvo ou ≥ 30% dos funcionários em cidades alvo,
  `saude.praca_fatia_minima`), funcionários e setor conhecidos. As outras ficam na fila de análise
  (contador "X analisadas · Y na fila"), que anda pela ordem do Score; a ficha da empresa mostra
  também as não analisadas, com o que falta. Empresas fora da praça continuam, com Score baixo e a
  etiqueta *fora da praça*. Por enquanto só Saúde: Linhas Financeiras e RE entram quando o fluxo de
  cada uma estiver pronto.
  O acompanhamento dos negócios abertos fica no Pipedrive; quando a oportunidade vira negócio lá,
  ela sai daqui. Colunas, nesta ordem: Score (o Potencial e o Score de Influência), Empresa
  (Cliente/Lead, funcionários e local), Em negociação, Oportunidade, Por quê (+ a favor, − contra,
  ? falta informação), Já tem conosco (com as caixas de Saúde), Quem decide e a ponte, Notícias e
  Próxima atividade. Acima da tabela, recolhidos, os *Critérios do Score em cada vertical*, com os
  pesos de `config/criterios.yaml`. O número de funcionários pode ser informado à mão (✎ na coluna
  Empresa ou na ficha): ele vale sobre o do LinkedIn e do Pipedrive por 180 dias; depois disso, a
  leitura do LinkedIn volta a atualizar. O local (cidade/UF) vem da Receita pelo CNPJ (inclusive o
  CNPJ achado no site da empresa, aceito só se o nome bater), do endereço da organização no Pipedrive
  ou da sede no LinkedIn. A cidade e a UF também podem ser informadas à mão (✎ na coluna Empresa ou
  na ficha): valem sobre todas as fontes e decidem a praça de Saúde sem gastar consulta. Filtros por vertical,
  cliente/lead e quem da equipe tem relação com a empresa. Passe o mouse nos títulos para ver
  como cada coluna é calculada. Mostra quem decide, a ponte e o porte (sem ponte por e-mail, o *contato no
  Pipedrive*: a pessoa do negócio aberto mais recente ou da organização). O cargo de qualquer pessoa pode
  ser informado à mão (✎): vale sobre o do LinkedIn e também é gravado no Pipedrive; *Criar
  atividade* cria a atividade na organização (e na pessoa escolhida) no Pipedrive.
- **Negócios em aberto**: todos os negócios abertos no Pipedrive nos funis de Linhas Financeiras,
  RE, Saúde e Pipo Saúde (sem Garantia, Flash e Canais Parceria), com funil, etapa, valor,
  responsável e próxima atividade. Filtros por funil, responsável e busca; marca as empresas que
  também estão em Oportunidades e, nas que esperam a análise, o que falta para entrar lá. Funcionários
  e cidade/UF podem ser informados ali mesmo (✎), com as mesmas regras da tela Oportunidades.
- **To-dos da semana**: atividades criadas pela plataforma, por responsável. Mostra as
  pendentes até sexta (incluindo as atrasadas) e as feitas na semana. Marcar como feita
  atualiza o Pipedrive.
- **Rotina**: como a plataforma se atualiza sozinha, de hora em hora: a ordem dos passos (Pipedrive,
  organizações excluídas no Pipedrive, e-mails, notícias, Receita, site, LinkedIn, scores e a revisão semanal
  do cadastro), o que cada um faz,
  os limites em vigor (consultas do LinkedIn por rodada e por dia, prazo para reler perfis, janela de
  e-mails) e a última execução de cada passo, com o erro quando falhou.
- **Qualidade** (o master e os heads): revisão semanal do cadastro do Pipedrive. *Cadastros suspeitos*: organizações
  cujo nome não parece de empresa (teste, "não tenho", pessoa física, nome sem letras), agrupadas por
  motivo, com seleção uma a uma ou do grupo inteiro e *Excluir selecionadas no Pipedrive* (negócios,
  pessoas e atividades ficam, só sem a organização) ou *Não é suspeita*. *Organizações
  duplicadas* (mesmo CNPJ, nome ou site), com sugestão de qual manter (mais negócios ganhos) e o
  botão *Mesclar no Pipedrive*, que junta negócios, pessoas, atividades e notas e não tem volta. Dá para
  escolher qual organização fica e *Excluir* uma organização do grupo no Pipedrive (negócios e pessoas ficam, sem ela). *Mesclar todas
  com nome idêntico* mescla de uma vez as organizações com o nome exatamente igual (letras, acentos,
  maiúsculas e pontuação; o CNPJ não é considerado, porque no Pipedrive ele é opcional e muitas vezes
  falta), conferindo cada nome no Pipedrive antes; nomes parecidos continuam na lista para análise.
  *Razão social*: nome da organização × razão social da Receita (pelo CNPJ), editável, com
  *Atualizar no Pipedrive*. Nada muda no Pipedrive sem aprovação.
- **Equipe** (só o master): convidar e remover pessoas, definir o papel (membro ou head; o head também
  acessa a Qualidade) e ligar a **leitura de e-mails** de cada uma
  (desligada por padrão; só para quem já entrou). A caixa também precisa estar no grupo
  `crosssell-equipe` do Microsoft 365; a tela mostra o resultado da última leitura.
- **Ficha da empresa**: seguros vigentes, pessoas e temperatura, histórico de produtos e notícias.

## Regras

- **Seguro vigente (Pipedrive)**: negócio **ganho** com *Fim Vigência* **depois de hoje**.
  Ganho com fim antes de hoje é *vencido*. Ganho sem fim de vigência não conta. Ganho que é
  cancelamento (título com "cancelamento" ou valor negativo) não conta.
- **Produto**: campos "Produtos Responsabilidade", "Produtos RE", "Produto Vertical Saúde" e
  "Produto Foco", nessa ordem. Sem nenhum deles, usa o título sem o ano.
- **Saúde** não tem fim de vigência (o contrato vale até o cliente cancelar): é cliente quem tem
  negócio de Saúde **ganho** no Pipedrive, apólice ativa no Zeca, "Já possui o seguro saúde na
  Genoa?" = Sim ou a caixa marcada na tabela. Quando cancelar, a equipe desmarca *Ainda é cliente
  Saúde* e a empresa vira reconquista.
- **Quem decide**: a área sai do cargo (Pipedrive) ou do título do LinkedIn. Saúde → RH/Pessoas/
  Benefícios; Linhas Financeiras → Financeiro, Jurídico, Riscos; RE → Operações, Riscos,
  Financeiro. Sem ninguém da área, vale o executivo (CEO, sócio). A ponte é o contato da empresa
  com relação mais forte com alguém da equipe.
- **Score de Influência** (0–100) = 50% relacionamento por e-mail (frequência, recência,
  reciprocidade e amplitude, ajustado pela temperatura) + 50% hierarquia do cargo (sócio/C-level 1,0;
  diretor/head 0,8; gerente/coordenador 0,55; demais 0,3).
- **Potencial** (0–100), pesos e palavras em [`config/criterios.yaml`](config/criterios.yaml)
  (editável. Rodando local, vale na próxima abertura da tela, sem reiniciar. Em produção, o arquivo vai
  dentro da imagem do Docker: faça o commit da mudança e rode `bash deploy/atualizar.sh` no servidor;
  editar a cópia do servidor não muda nada. Assim o git guarda quem mudou cada critério e quando):
  - *Saúde*: influência 30, funcionários (ou vidas) 25, qualificação do time 20 (startups,
    fintechs, fundos, multinacionais, tecnologia, farmacêuticas × indústria, transporte, varejo,
    restaurantes), localização 15 (interior perde para a Unimed local) e RH estruturado 10.
  - *Linhas Financeiras*: influência 35 e encaixe do produto 65. E&O: serviço intelectual
    (advocacia, contabilidade, tecnologia, saúde, consultoria). D&O: gestão profissional (venture
    capital, notícias de M&A ou investimento (comprou, foi comprada, fusão, recebeu investimento;
    lista `ma`), aporte/conselho nas notícias, S.A., diretoria executiva, porte); fundos e gestoras:
    IMI. Cyber: CTO ou DPO na empresa, setor com muitos dados.
  - *RE*: influência 30, perfil 45 (galpões, indústrias, transportadoras; empresa grande em
    escritório: empresarial e fiança) e porte 25.
  - Dados usados: setor e descrição do LinkedIn, CNAE e natureza jurídica da Receita, cidade/UF,
    funcionários, cargos das pessoas e notícias.

## Integrações

| Fonte | Como entra | O que traz |
|---|---|---|
| Pipedrive | API v2 (sincronização incremental) | organizações, pessoas, negócios, etapas, usuários; escreve atividades |
| Zeca | importação de CSV/XLSX | apólices de Saúde (ausência numa carga completa = cancelada/migrou) |
| Microsoft 365 | Microsoft Graph (permissões de aplicativo `Mail.Read` e `Mail.Send`) | metadados dos e-mails dos usuários ativos + texto das respostas recebidas |

Um e-mail é ligado à empresa pelo endereço da pessoa já cadastrada ou pelo domínio. Valem o domínio do site,
o domínio corporativo dos contatos da empresa (ex.: Pro-Eficiência, site intergado.com.br e contatos
@pontaagro.com) e os domínios descobertos de graça (passo `dominios` da rotina, nas empresas com negócio aberto
em LF, RE, Saúde e Pipo, 1x a cada 180 dias): para onde o site redireciona, os e-mails que aparecem no site, o
site da página do LinkedIn e o e-mail da Receita quando lembra o nome da empresa (muitas vezes é o do
contador). Um domínio de empresas diferentes não decide nada. Quando um domínio passa a ser
conhecido, os e-mails já lidos dele são religados à empresa.
| Claude API | `claude-opus-5-5`, saída estruturada | temperatura de cada contato (o texto dos e-mails não é guardado) |
| Google Notícias | RSS | manchetes recentes de cada empresa da tabela |
| LinkedIn | Linked API, chamada direto (`api.linkedapi.io`) | perfis, cargos, decisores, funcionários da área que decide, posts, nº de funcionários; aviso de contato que mudou de empresa |
| Receita Federal (BrasilAPI) | API pública | porte, CNAE, capital social, sócios |

### LinkedIn (Linked API)

É automático, sem planilha e sem n8n. A rotina de hora em hora chama `crosssell linkedin`, que:

1. confere as consultas em andamento na Linked API e aplica as que terminaram;
2. inicia até `LINKEDIN_LOTE` novas (10), sem passar de `LINKEDIN_LIMITE_DIA` (50) em 24 h,
   na ordem do Score (Oportunidades primeiro, depois as empresas dos negócios abertos).

**Cotas por vertical** (consultas por dia): Saúde 30 (`LINKEDIN_COTA_SAUDE`, das quais 12 para
quem decide, `LINKEDIN_COTA_SAUDE_PESSOAS`), Linhas Financeiras 10 (`LINKEDIN_COTA_LF`) e RE 10
(`LINKEDIN_COTA_RE`). Paga a vertical da oportunidade de maior Score da empresa, e o que for lido
serve a todas. A cada rodada as cotas com saldo se alternam; a vaga que uma vertical não usa (por
não ter o que consultar) passa para as outras.

**Fluxo de Saúde**, um passo por vez:

1. *Empresa*: acha a página (link no site, de graça, ou busca pelo nome) e lê funcionários, setor,
   sede e o urn. Não gasta consulta se os funcionários foram informados à mão (valem 180 dias) e o
   setor já é conhecido (LinkedIn ou CNAE).
2. *Praça*: cidade informada à mão ou cidade alvo (`saude.metropoles`) decide sozinha. Fora disso,
   o Sales Navigator traz a lista de funcionários com o local de cada um; com pelo menos
   `saude.praca_fatia_minima` (30%) em cidades alvo a empresa vale, senão fica *fora da praça*.
3. *Quem decide*: só na praça, a lista do Sales Navigator filtrada pelos cargos de
   `saude.linkedin_cargos` (diretor de RH, CHRO, Head de People, Founder, CFO). Atualiza o cargo de
   quem já conhecemos pelos e-mails e cadastra quem falta. Sem Sales Navigator, usa a lista comum de
   funcionários da página.

Linhas Financeiras e RE seguem, por enquanto, a leitura geral: **ler** (perfil, ou página da
empresa com decisores e posts), **buscar** (procura pelo nome; a pessoa só é aceita se o nome e a
empresa conferirem no título, e a empresa pelo domínio ou pelo nome com local no Brasil) e
**funcionários** (lista de até 50 pessoas da página da empresa, quando falta alguém da área que
decide; descarta quem cita outra organização, como investidores e conselheiros).

Releitura a cada 180 dias (`LINKEDIN_VALIDADE_DIAS`), também para a praça e quem decide; quem não
foi encontrado volta a ser procurado depois de 30 dias e pode receber o endereço à mão na tela
Equipe.

**Sales Navigator** (`LINKEDIN_SALES_NAVIGATOR`, desligado por padrão): antes de ligar, teste no
servidor com algumas empresas (gasta até 3 consultas por empresa e mostra o que voltou):

```bash
docker compose exec web crosssell linkedin-teste 123 "Nome da Empresa"   # só mostra
docker compose exec web crosssell linkedin-teste 123 --aplicar           # grava praça e pessoas
```

Enquanto espera, o comando mostra a cada minuto se a consulta está na fila da conta ou rodando. Ele espera até
60 minutos (`--minutos`); se desistir, a consulta já paga fica guardada e é reaproveitada ao rodar de novo
(uma consulta de antes desta versão entra com `--workflow wf-...`).

Se a praça e as pessoas fizerem sentido, ponha `LINKEDIN_SALES_NAVIGATOR=true` no `deploy/.env`.

Tokens: `LINKED_API_TOKEN` e `LINKED_API_IDENTIFICATION_TOKEN` (painel da Linked API). O caminho
antigo pelo n8n continua disponível se esses tokens não forem definidos
([`docs/linkedin-n8n.md`](docs/linkedin-n8n.md)).

## Instalação

```bash
pip install -e ".[dev]"
cp .env.example .env              # tokens: Pipedrive, Microsoft 365, ANTHROPIC_API_KEY
crosssell initdb                  # aplica as migrações do banco (não cria usuários)
crosssell criar-master rodrigo.pedroni@innoaseguros.com.br "Rodrigo Pedroni"
crosssell serve                   # http://localhost:8000
```

O master entra, abre **Equipe** e gera o link de convite de cada pessoa. A pessoa cria a
própria senha pelo link, que vale 7 dias e só pode ser usado uma vez.

## Rotina (cron)

Uma entrada só faz tudo, sem ação manual: `crosssell rotina`, de hora em hora. Ou, em separado:

```bash
crosssell pipedrive --dias 2      # a cada hora: negócios + status das atividades
crosssell emails --dias 2         # a cada hora: e-mails + temperatura
crosssell cnpj-sites              # a cada hora: CNPJ no site de empresas sem CNPJ (só se o nome bater)
crosssell dominios                # a cada hora: outros domínios de e-mail (site, redirecionamento)
crosssell noticias                # diário
crosssell linkedin                # a cada hora: aplica resultados e inicia o próximo lote (limite de 24 h)
crosssell recalcular              # diário
crosssell qualidade               # semanal (a rotina já roda): duplicadas e razão social
crosssell importar zeca arquivo.xlsx   # quando houver nova exportação
```

## Prévia sem servidor

```bash
DATABASE_URL=sqlite:///demo.db python scripts/demo.py            # empresas fictícias
DATABASE_URL=sqlite:///demo.db python scripts/exportar_preview.py preview.html --exemplo
```

Gera um HTML único com a mesma interface do app (`crosssell/web/app.html`) e os dados
embutidos. Nele as ações ficam só no navegador.

## Segurança e LGPD

- Senhas com scrypt. Sessões são tokens aleatórios, e o banco guarda só o hash, então dá para revogar.
- Os cookies são `HttpOnly`, `SameSite=Lax` e `Secure`. As chamadas que alteram dados exigem um cabeçalho próprio do app.
- Remover o acesso de alguém encerra as sessões dessa pessoa e para a leitura dos e-mails dela.
- Dos e-mails ficam guardados só os metadados. O texto das respostas recebidas vai para a
  classificação de temperatura e é descartado. Formalize a base legal (legítimo interesse) com o
  DPO e informe a equipe. A tela de convite já avisa a pessoa.

## Desenvolvimento

```bash
pytest
```

### Mudanças no banco (migrações)

O banco segue as migrações do Alembic em `crosssell/migrations/versions/`. Elas são aplicadas
sozinhas no `crosssell initdb`, ao subir o servidor e em todo comando do `crosssell` (no servidor, o
`deploy/atualizar.sh` já cuida disso). Depois de mudar `crosssell/models.py`:

```bash
alembic revision --autogenerate -m "o que mudou"   # gera a migração comparando o modelo com o banco do .env
```

Revise o arquivo gerado e faça o commit junto com o modelo. O teste `tests/test_migracoes.py` falha se
um modelo mudar sem migração. Bancos criados antes das migrações são marcados como `0001` na primeira
execução, sem perder dados.

## Pendências para produção

- [ ] Saída do servidor para a internet: `innoaseguros.pipedrive.com`, `graph.microsoft.com`,
      `login.microsoftonline.com`, `api.anthropic.com`, `api.linkedapi.io`, `news.google.com`,
      `brasilapi.com.br` e o site de cada empresa (a rotina procura nele o link do LinkedIn).
- [ ] Token de API do Pipedrive (usuário admin), app registration no Microsoft 365 (`Mail.Read` e
      `Mail.Send`, com `EMAIL_REMETENTE` para o link de nova senha) e chave da Claude API.
- [ ] Tokens da Linked API (gerar novos antes de produção; os de teste foram expostos).
- [ ] Exportação real do Zeca para ajustar os cabeçalhos em `config/verticais.yaml`.
- [ ] Hospedagem: EC2 `zeca-server`, com Postgres no Docker (roteiro em [`deploy/LEIA-ME.md`](deploy/LEIA-ME.md)).
