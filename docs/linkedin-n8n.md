# LinkedIn: planilha + n8n + Linked API

```
Plataforma ──(1) escreve──▶ Google Sheet "Alvos"
                                   │
                         (2) n8n lê, chama a Linked API
                                   ▼
Plataforma ◀──(4) lê────── Google Sheet "Resultados"  ◀──(3) n8n grava
```

## 1. Planilha

1. Crie uma planilha no Google Sheets com duas abas: **Alvos** e **Resultados**.
2. No Google Cloud, crie uma **conta de serviço**, ative a *Google Sheets API* e baixe a chave JSON.
3. Compartilhe a planilha com o e-mail da conta de serviço como **Editor**.
4. No servidor da plataforma, configure:
   ```
   GOOGLE_SHEETS_ID=<id que aparece na URL da planilha>
   GOOGLE_SERVICE_ACCOUNT_FILE=/caminho/conta-servico.json
   ```
5. Na primeira linha da aba **Resultados**, escreva o cabeçalho exatamente assim:
   ```
   id_alvo | tipo | linkedin_url | nome | headline | cargo_atual | empresa_atual | localizacao | setor | funcionarios | site | sede | decisores | posts | capturado_em | erro
   ```

A plataforma reescreve a aba **Alvos** a cada envio. As colunas são `id_alvo`, `tipo`
(pessoa/empresa), `nome`, `empresa`, `cargo`, `email`, `linkedin_url`, `site`, `cnpj`, `motivo`
e `pedido_em`. Entram as empresas e os contatos dos negócios abertos da tabela que nunca foram
lidos no LinkedIn ou cuja última leitura tem mais de 90 dias.

O envio é feito pelo botão *Enviar alvos para a planilha* (aba Equipe) ou por
`crosssell linkedin-exportar`.

## 2. Fluxo no n8n

Pré-requisito: o nó da comunidade `n8n-nodes-linked-api` instalado e uma credencial da Linked API.

| # | Nó | Configuração |
|---|---|---|
| 1 | **Schedule Trigger** | todo dia útil, por exemplo às 7h |
| 2 | **Google Sheets: Get Rows** ("Ler Alvos") | aba `Alvos` |
| 3 | **Google Sheets: Get Rows** ("Ler Resultados") | aba `Resultados` |
| 4 | **Merge** | modo *Keep Non-Matches*, chave `id_alvo`: descarta alvos que já têm resultado |
| 5 | **Limit** | 25 a 40 itens por execução (ritmo humano para a conta conectada) |
| 6 | **Switch** em `tipo` | saída `pessoa` e saída `empresa` |
| 7a | **Linked API: Fetch Person** | URL = `linkedin_url`; se vier vazia, antes use *Search People* com `nome` + `empresa` e pegue o 1º resultado; ative *retrieve experience* |
| 7b | **Linked API: Fetch Company** | URL = `linkedin_url` ou *Search Companies* com `nome`; ative *retrieve decision makers* e *retrieve posts* (até 5) |
| 8 | **Code** ("Mapear") | script abaixo |
| 9 | **Google Sheets: Append Row** | aba `Resultados`, *Map Automatically* |

Nos nós 7a e 7b, ligue *On Error → Continue (using error output)* e envie a saída de erro para um
**Set** com `id_alvo`, `tipo` e `erro = {{$json.error.message}}`, que segue para o nó 9. Assim o
alvo não fica preso.

### Script do nó "Mapear" (Code, *Run Once for Each Item*, JavaScript)

```javascript
// Converte a resposta da Linked API para as colunas da aba Resultados.
// Os nomes dos campos variam entre versões: o pick() tenta alternativas.
// Confira a saída do nó 7a/7b na primeira execução e ajuste se precisar.
const alvo = $('Ler Alvos').item.json;
const r = $json.data ?? $json;
const pick = (...caminhos) => {
  for (const c of caminhos) {
    const v = c.split('.').reduce((o, k) => (o == null ? undefined : o[k]), r);
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return '';
};
const exp0 = (pick('experiences', 'experience') || [])[0] || {};
const lista = (v) => (Array.isArray(v) ? v : []);

return {
  id_alvo: alvo.id_alvo,
  tipo: alvo.tipo,
  linkedin_url: pick('publicUrl', 'url', 'profileUrl', 'companyUrl'),
  nome: pick('name', 'fullName'),
  headline: pick('headline'),
  cargo_atual: exp0.position || exp0.title || '',
  empresa_atual: exp0.companyName || exp0.company || '',
  localizacao: pick('location'),
  setor: pick('industry'),
  funcionarios: pick('employeesCount', 'employeeCount', 'size'),
  site: pick('website'),
  sede: pick('headquarters', 'location'),
  decisores: JSON.stringify(lista(pick('dms', 'decisionMakers')).slice(0, 20).map(d => ({
    nome: d.name, headline: d.headline || '', linkedin_url: d.publicUrl || d.url || '',
  }))),
  posts: JSON.stringify(lista(pick('posts')).slice(0, 5).map(p => ({
    texto: p.text || '', data: p.time || p.date || '', url: p.url || '',
  }))),
  capturado_em: new Date().toISOString().slice(0, 19),
  erro: '',
};
```

## 3. Leitura pela plataforma

Botão *Ler resultados da planilha* (aba Equipe) ou `crosssell linkedin-importar`. O que é atualizado:

- **Pessoas**: perfil, headline e cargo (o cargo só é preenchido quando estava vazio). Se a
  empresa atual no LinkedIn for outra, a tabela avisa: *"LinkedIn indica que Ana hoje está em X:
  confirmar o contato"*.
- **Empresas**: perfil, setor, número de funcionários, site e sede.
- **Decisores** (até 20 por empresa): viram pessoas da empresa com a origem "LinkedIn". Os que
  ainda não têm relação aparecem no *Por quê*: *"decisor no LinkedIn sem relação ainda: Marta
  Reis (Diretora Jurídica)"*.
- **Posts da empresa** (até 5): entram na coluna Notícias como "Post no LinkedIn".

Conferência: se o nome do perfil encontrado não bate com o da plataforma (primeiro e último
nome), o perfil é descartado e contado como "não confere". Isso acontece quando a busca por nome
traz um homônimo. Reimportar a mesma planilha não duplica nada.

## Agenda sugerida

| Horário | Ação |
|---|---|
| 6h30 | `crosssell linkedin-exportar` |
| 7h | fluxo do n8n |
| 8h | `crosssell linkedin-importar` |

## Cuidados

- A Linked API opera uma conta real do LinkedIn por um navegador na nuvem. Isso contraria os
  termos de uso do LinkedIn, e a conta conectada pode ser restringida. Mantenha volumes baixos
  (nó *Limit*) e decida conscientemente qual conta conectar.
- Guarde apenas dados profissionais. Registre essa finalidade (prospecção B2B e gestão de
  relacionamento) junto com o restante do tratamento de dados da plataforma.
