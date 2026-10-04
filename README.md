# Plataforma Cross Sell · Innoa

Ferramenta tática para a reunião semanal (sexta-feira) das verticais **Linhas Financeiras, Saúde e
Ramos Elementares**. Mostra os negócios abertos com tudo o que ajuda a decidir o próximo passo:
o que o cliente já tem conosco, a temperatura do contato, notícias e quem tem relação. A partir
dela a equipe cria atividades no Pipedrive e acompanha os to-dos da semana.

## Telas

- **Login**: e-mail e senha, com *Esqueci minha senha* (link de uso único por e-mail, vale 1 hora;
  o master também gera esse link na tela Equipe).
- **Negócios**: uma linha por negócio **aberto** nos funis Linhas Financeiras (1), RE (29),
  Saúde (23), Pipo Saúde (34) e Canais Parceria (39). Os funis Garantia (40), Flash
  Benefícios (38) e M&A (31) ficam fora da tabela. Ordenada pelo **Potencial**. Colunas:
  - **Potencial** (critérios da vertical) e, abaixo, o **Score de Influência** do contato.
  - **Cliente/Lead**: é *cliente* quem tem ao menos um seguro vigente.
  - **Temperatura**: Pouca, Média ou Muita abertura, calculada pela IA a partir da escrita do contato.
  - **Seguros vigentes**, por produto (D&O, Cyber, Empresarial…). Inclui a caixa *Cliente
    Saúde (fora da planilha)* para quem não aparece na exportação do Zeca.
  - **Negócio aberto**: funil, título, etapa e valor.
  - **Por quê**: só o que não aparece em outra coluna: critérios a favor (+), contra (−) e sem
    informação (?), reconquista e contato que mudou de empresa.
  - **Notícias**: principais manchetes recentes.
  - **Quem decide** a vertical do negócio (pela área do cargo), com a influência de cada pessoa e a
    **ponte** quando ninguém dessa área tem relação com a equipe.
  - **Próximo passo**: a próxima atividade e o botão *Criar atividade*.
- **Oportunidades**: clientes (seguro vigente) e leads em negociação numa vertical que ainda não
  têm seguro nem negócio aberto em outra (ex.: tem D&O, não tem Saúde), ordenados pelo Potencial
  na vertical da oportunidade. Mostra quem decide, a ponte e o porte; *Criar
  atividade* cria a atividade na organização (e na pessoa escolhida) no Pipedrive.
- **To-dos da semana**: atividades criadas pela plataforma, por responsável. Mostra as
  pendentes até sexta (incluindo as atrasadas) e as feitas na semana. Marcar como feita
  atualiza o Pipedrive.
- **Equipe** (só o master): convidar e remover pessoas. Usuário ativo tem os e-mails lidos.
- **Ficha da empresa**: seguros vigentes, pessoas e temperatura, histórico de produtos e notícias.

## Regras

- **Seguro vigente (Pipedrive)**: negócio **ganho** com *Fim Vigência* **depois de hoje**.
  Ganho com fim antes de hoje é *vencido*. Ganho sem fim de vigência não conta. Ganho que é
  cancelamento (título com "cancelamento" ou valor negativo) não conta.
- **Produto**: campos "Produtos Responsabilidade", "Produtos RE", "Produto Vertical Saúde" e
  "Produto Foco", nessa ordem. Sem nenhum deles, usa o título sem o ano.
- **Saúde**: é cliente quem tem apólice ativa no Zeca, quem foi marcado na caixa da tabela ou
  quem tem "Já possui o seguro saúde na Genoa?" = Sim em algum negócio.
- **Quem decide**: a área sai do cargo (Pipedrive) ou do título do LinkedIn. Saúde → RH/Pessoas/
  Benefícios; Linhas Financeiras → Financeiro, Jurídico, Riscos; RE → Operações, Riscos,
  Financeiro. Sem ninguém da área, vale o executivo (CEO, sócio). A ponte é o contato da empresa
  com relação mais forte com alguém da equipe.
- **Score de Influência** (0–100) = 50% relacionamento por e-mail (frequência, recência,
  reciprocidade e amplitude, ajustado pela temperatura) + 50% hierarquia do cargo (sócio/C-level 1,0;
  diretor/head 0,8; gerente/coordenador 0,55; demais 0,3).
- **Potencial** (0–100), pesos e palavras em [`config/criterios.yaml`](config/criterios.yaml)
  (editável; vale sem reiniciar):
  - *Saúde*: influência 30, funcionários (ou vidas) 25, qualificação do time 20 (startups,
    fintechs, fundos, multinacionais, tecnologia, farmacêuticas × indústria, transporte, varejo,
    restaurantes), localização 15 (interior perde para a Unimed local) e RH estruturado 10.
  - *Linhas Financeiras*: influência 35 e encaixe do produto 65. E&O: serviço intelectual
    (advocacia, contabilidade, tecnologia, saúde, consultoria). D&O: gestão profissional (venture
    capital, aporte/conselho nas notícias, S.A., diretoria executiva, porte); fundos e gestoras:
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
| Claude API | `claude-opus-5-5`, saída estruturada | temperatura de cada contato (o texto dos e-mails não é guardado) |
| Google Notícias | RSS | manchetes recentes de cada empresa da tabela |
| LinkedIn | Linked API, chamada direto (`api.linkedapi.io`) | perfis, cargos, decisores, funcionários da área que decide, posts, nº de funcionários; aviso de contato que mudou de empresa |
| Receita Federal (BrasilAPI) | API pública | porte, CNAE, capital social, sócios |

### LinkedIn (Linked API)

É automático, sem planilha e sem n8n. A rotina de hora em hora chama `crosssell linkedin`, que:

1. confere as consultas em andamento na Linked API e aplica as que terminaram;
2. inicia até `LINKEDIN_LOTE` novas (10), sem passar de `LINKEDIN_LIMITE_DIA` (50) em 24 h,
   na ordem da tabela (maior score primeiro) e depois das Oportunidades.

Tipos de consulta: **ler** (perfil, ou página da empresa com decisores e posts), **buscar**
(procura pelo nome; a pessoa só é aceita se o nome e a empresa conferirem no título, e a empresa
pelo domínio ou pelo nome com local no Brasil) e **área** (funcionários com cargo da área que
decide, quando ainda não conhecemos ninguém dela). Releitura a cada 90 dias; quem não foi
encontrado volta a ser procurado depois de 30 dias e pode receber o endereço à mão na tela
Equipe. O link da empresa também é procurado de graça no site dela.

Tokens: `LINKED_API_TOKEN` e `LINKED_API_IDENTIFICATION_TOKEN` (painel da Linked API). O caminho
antigo pelo n8n continua disponível se esses tokens não forem definidos
([`docs/linkedin-n8n.md`](docs/linkedin-n8n.md)).

## Instalação

```bash
pip install -e ".[dev]"
cp .env.example .env              # tokens: Pipedrive, Microsoft 365, ANTHROPIC_API_KEY
crosssell initdb                  # cria as tabelas e a equipe inicial do config
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
crosssell noticias                # diário
crosssell linkedin                # a cada hora: aplica resultados e inicia o próximo lote (limite de 24 h)
crosssell recalcular              # diário
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

## Pendências para produção

- [ ] Liberar a rede do ambiente para `api.pipedrive.com`, `graph.microsoft.com`,
      `api.anthropic.com`, `api.linkedapi.io`, `news.google.com` e `brasilapi.com.br`.
- [ ] Token de API do Pipedrive (usuário admin), app registration no Microsoft 365 (`Mail.Read` e
      `Mail.Send`, com `EMAIL_REMETENTE` para o link de nova senha) e chave da Claude API.
- [ ] Tokens da Linked API (gerar novos antes de produção; os de teste foram expostos).
- [ ] Exportação real do Zeca para ajustar os cabeçalhos em `config/verticais.yaml`.
- [ ] Hospedagem (Postgres + container com HTTPS).
