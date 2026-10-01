# Plataforma Cross Sell · Innoa

Ferramenta tática para a reunião semanal (sexta-feira) das verticais **Linhas Financeiras, Saúde e
Ramos Elementares**. Mostra os negócios abertos com tudo o que ajuda a decidir o próximo passo:
o que o cliente já tem conosco, a temperatura do contato, notícias e quem tem relação. A partir
dela a equipe cria atividades no Pipedrive e acompanha os to-dos da semana.

## Telas

- **Negócios**: uma linha por negócio **aberto** nos funis Linhas Financeiras (1), RE (29),
  Saúde (23), Pipo Saúde (34) e Canais Parceria (39). Os funis Garantia (40), Flash
  Benefícios (38) e M&A (31) ficam fora da tabela. Colunas:
  - **Cliente/Lead**: é *cliente* quem tem ao menos um seguro vigente.
  - **Temperatura**: Pouca, Média ou Muita abertura, calculada pela IA a partir da escrita do contato.
  - **Seguros vigentes**, por produto (D&O, Cyber, Empresarial…). Inclui a caixa *Cliente
    Saúde (fora da planilha)* para quem não aparece na exportação do Zeca.
  - **Negócio aberto**: funil, título, etapa e valor.
  - **Por quê**: cross sell, renovação próxima, reconquista e acesso a decisor.
  - **Notícias**: principais manchetes recentes.
  - **Relação**: quem apresenta (tem mais e-mails com o contato) e quem atende (dono do negócio).
  - **Próximo passo**: a próxima atividade e o botão *Criar atividade*.
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
- **Score** = relacionamento + vínculo + momento + porte. Em Saúde os pesos são 35/25/15/25;
  nas demais verticais, 40/30/20/10.
  - *Relacionamento*: frequência, recência, reciprocidade e amplitude dos e-mails com o
    contato, ajustados pela temperatura.
  - *Vínculo*: seguros vigentes em outras verticais (cross sell) e/ou na mesma.
  - *Momento*: renovação de algum seguro vigente em 30–120 dias.
  - *Porte*: em negócios de Saúde, as vidas informadas no negócio ("Quantidade de Vidas" ou
    "Faixa de Vidas"). Nos demais, o número de funcionários, e o do LinkedIn prevalece sobre o
    do Pipedrive. A escala é logarítmica: 10 → 0,33, 100 → 0,67, 1.000 ou mais → 1.

## Integrações

| Fonte | Como entra | O que traz |
|---|---|---|
| Pipedrive | API v2 (sincronização incremental) | organizações, pessoas, negócios, etapas, usuários; escreve atividades |
| Zeca | importação de CSV/XLSX | apólices de Saúde (ausência numa carga completa = cancelada/migrou) |
| Microsoft 365 | Microsoft Graph (permissão de aplicativo `Mail.Read`) | metadados dos e-mails dos usuários ativos + texto das respostas recebidas |
| Claude API | `claude-opus-5-5`, saída estruturada | temperatura de cada contato (o texto dos e-mails não é guardado) |
| Google Notícias | RSS | manchetes recentes de cada empresa da tabela |
| LinkedIn | webhook do n8n + Linked API, retorno por API | perfis, cargos, decisores, posts; aviso de contato que mudou de empresa |
| Receita Federal (BrasilAPI) | API pública | porte, CNAE, capital social, sócios |

### LinkedIn (n8n + Linked API)

É automático, sem planilha. A cada rotina, a plataforma envia ao webhook do n8n um lote com as
empresas e os contatos dos negócios abertos que ainda não foram lidos no LinkedIn. O n8n
consulta a Linked API e devolve cada resultado em `POST /api/integracoes/linkedin/resultados`,
autenticado por token. A plataforma atualiza perfis, headline, setor, número de funcionários,
decisores e posts. O perfil só é aceito se o nome conferir.
Fluxo do n8n, contrato da API e script de mapeamento: [`docs/linkedin-n8n.md`](docs/linkedin-n8n.md).

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
crosssell linkedin                # diário: envia o próximo lote ao n8n
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
      `api.anthropic.com`, `news.google.com` e `brasilapi.com.br`.
- [ ] Token de API do Pipedrive (usuário admin), app registration no Microsoft 365 e chave da Claude API.
- [ ] Fluxo do n8n (`docs/linkedin-n8n.md`), token compartilhado e endereço público da plataforma
      (o n8n precisa alcançá-la para devolver os resultados).
- [ ] Exportação real do Zeca para ajustar os cabeçalhos em `config/verticais.yaml`.
- [ ] Hospedagem (Postgres + container com HTTPS).
