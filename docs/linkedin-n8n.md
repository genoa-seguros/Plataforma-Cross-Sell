# LinkedIn: plataforma ⇄ n8n ⇄ Linked API

Automático, sem planilha e sem ação manual:

```
           (1) levanta os alvos                    (3) consulta cada alvo
Plataforma ─────(2) POST webhook──────▶ n8n ──────────────────────▶ Linked API
    ▲                                    │
    └──(4) POST /api/integracoes/linkedin/resultados (um por alvo)
```

1. A cada rotina (`crosssell rotina` ou `crosssell linkedin`), a plataforma separa até
   `LINKEDIN_LOTE` alvos (padrão 30). São as empresas e os contatos dos negócios abertos que
   nunca foram lidos no LinkedIn ou foram lidos há mais de 90 dias.
2. A plataforma chama o webhook do n8n com o lote e o endereço de retorno.
3. O n8n responde na hora e consulta a Linked API alvo por alvo.
4. O n8n devolve cada resultado à plataforma, que atualiza tudo na mesma hora.

Um alvo enviado fica "em andamento" por 36 horas. Nesse período ele não é reenviado. Se não
voltar dentro desse prazo, entra de novo na fila.

## Configuração

### Plataforma (`.env`)

```
N8N_LINKEDIN_WEBHOOK_URL=https://<seu-n8n>/webhook/crosssell-linkedin
N8N_TOKEN=<token longo e aleatório; o mesmo vai no n8n>
PLATAFORMA_URL=https://<endereço público da plataforma>
LINKEDIN_LOTE=30
```

Para gerar um token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### n8n

Crie uma credencial **Header Auth** com nome `Authorization` e valor `Bearer <N8N_TOKEN>`.
Ela é usada na entrada (nó 1) e na saída (nó 7). Pré-requisito: o nó da comunidade
`n8n-nodes-linked-api` instalado e uma credencial da Linked API.

| # | Nó | Configuração |
|---|---|---|
| 1 | **Webhook** | POST, path `crosssell-linkedin`, *Authentication: Header Auth* (credencial acima), *Respond: Immediately* |
| 2 | **Split Out** | campo `body.alvos` |
| 3 | **Loop Over Items** | lote de 1 (um alvo por vez) |
| 4 | **Wait** | 20 a 60 segundos (ritmo humano para a conta conectada) |
| 5 | **Switch** em `tipo` | saídas `pessoa` e `empresa` |
| 6a | **Linked API: Fetch Person** | URL = `linkedin_url`; se vier vazia, antes use *Search People* com `nome` + `empresa` e pegue o 1º resultado; ative *retrieve experience* |
| 6b | **Linked API: Fetch Company** | URL = `linkedin_url` ou *Search Companies* com `nome`; ative *retrieve decision makers* e *retrieve posts* (até 5) |
| 6c | **Code** ("Mapear") | script abaixo, depois de 6a e 6b |
| 7 | **HTTP Request** | POST para `{{ $('Webhook').first().json.body.callback_url }}`, *Authentication: Header Auth* (mesma credencial), *Body: JSON* = `{{ $json }}`; volta para o nó 3 |

Nos nós 6a e 6b, ligue *On Error → Continue (using error output)* e envie a saída de erro para um
**Set** com `id_alvo`, `tipo` e `erro = {{ $json.error.message }}`, que segue para o nó 7. Assim
a plataforma sabe que aquele alvo falhou.

### Script do nó "Mapear" (Code, *Run Once for Each Item*, JavaScript)

```javascript
// Converte a resposta da Linked API no formato que a plataforma espera.
// Os nomes dos campos variam entre versões: o pick() tenta alternativas.
// Confira a saída dos nós 6a/6b na primeira execução e ajuste se precisar.
const alvo = $('Loop Over Items').item.json;
const r = $json.data ?? $json;
const pick = (...caminhos) => {
  for (const c of caminhos) {
    const v = c.split('.').reduce((o, k) => (o == null ? undefined : o[k]), r);
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return '';
};
const lista = (v) => (Array.isArray(v) ? v : []);
const exp0 = lista(pick('experiences', 'experience'))[0] || {};

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
  decisores: lista(pick('dms', 'decisionMakers')).slice(0, 20).map(d => ({
    nome: d.name, headline: d.headline || '', linkedin_url: d.publicUrl || d.url || '',
  })),
  posts: lista(pick('posts')).slice(0, 5).map(p => ({
    texto: p.text || '', data: p.time || p.date || '', url: p.url || '',
  })),
  capturado_em: new Date().toISOString().slice(0, 19),
};
```

## Contrato da integração

**Disparo (plataforma → n8n)**: `POST N8N_LINKEDIN_WEBHOOK_URL`, cabeçalho
`Authorization: Bearer <N8N_TOKEN>`.

```json
{
  "lote_id": "20261001063000-a1b2c3",
  "callback_url": "https://<plataforma>/api/integracoes/linkedin/resultados",
  "alvos": [
    {"id_alvo": "E12", "tipo": "empresa", "nome": "Metalúrgica Alfa", "empresa": "Metalúrgica Alfa Ltda",
     "linkedin_url": "", "site": "https://alfa.com.br", "cnpj": "14069185000103", "cargo": "", "email": "",
     "motivo": "RE: Empresarial 2026"},
    {"id_alvo": "P40", "tipo": "pessoa", "nome": "Ana Souza", "empresa": "Metalúrgica Alfa Ltda",
     "cargo": "CFO", "email": "ana@alfa.com.br", "linkedin_url": "", "site": "", "cnpj": "",
     "motivo": "contato do negócio"}
  ]
}
```

**Retorno (n8n → plataforma)**: `POST {callback_url}`, mesmo cabeçalho. O corpo pode ser um
resultado, uma lista ou `{"resultados": [...]}`. Campos: `id_alvo`, `tipo`, `linkedin_url`,
`nome`, `headline`, `cargo_atual`, `empresa_atual`, `localizacao`, `setor`, `funcionarios`,
`site`, `sede`, `decisores` (lista de `{nome, headline, linkedin_url}`), `posts` (lista de
`{texto, data, url}`), `capturado_em` e, em caso de falha, `erro`.

Respostas: `401` se o token estiver errado; `200` com a contagem do que foi aplicado.

## O que a plataforma faz com o retorno

- **Pessoas**: perfil, headline e cargo (o cargo só é preenchido quando estava vazio). Se a
  empresa atual no LinkedIn for outra, a tabela avisa: *"LinkedIn indica que Ana hoje está em X:
  confirmar o contato"*.
- **Empresas**: perfil, setor, número de funcionários, site e sede.
- **Decisores**: viram pessoas da empresa com a origem "LinkedIn". Os que ainda não têm relação
  aparecem no *Por quê*.
- **Posts**: entram na coluna Notícias como "Post no LinkedIn".
- **Conferência de nome**: se o perfil não tem o mesmo primeiro e último nome da pessoa na
  plataforma (homônimo encontrado pela busca), ele é descartado e contado como "não confere".
- **Reenvio**: um resultado já aplicado não é aplicado de novo.

## Cuidados

- A Linked API opera uma conta real do LinkedIn por um navegador na nuvem. Isso contraria os
  termos de uso do LinkedIn, e a conta conectada pode ser restringida. O lote e o nó Wait
  controlam o ritmo.
- Guarde apenas dados profissionais e registre a finalidade (prospecção B2B e gestão de
  relacionamento) junto com o restante do tratamento de dados da plataforma.
