"""Gera n8n/crosssell-linkedin.json (fluxo importável no n8n). Rode: python n8n/gerar_fluxo.py"""

import json
import uuid
from pathlib import Path

MAPEAR_JS = r"""// Converte a resposta da Linked API no formato que a plataforma espera.
// Os nomes dos campos variam entre versões: o pick() tenta alternativas.
// Na primeira execução, confira a saída dos nós da Linked API e ajuste se algum campo vier vazio.
const alvo = $('Separar alvos').first().json;  // a plataforma envia um alvo por execução
// Depois do Wait, o retorno da Linked API vem em body (às vezes dentro de data/result).
const b = $json.body ?? $json;
const r = b.data ?? b.result ?? b;
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
  json: {
    id_alvo: alvo.id_alvo,
    tipo: alvo.tipo,
    linkedin_url: pick('publicUrl', 'url', 'profileUrl', 'companyUrl'),
    nome: pick('name', 'fullName'),
    headline: pick('headline'),
    cargo_atual: exp0.position || exp0.title || '',
    empresa_atual: exp0.companyName || exp0.company || '',
    localizacao: pick('location'),
    setor: pick('industry'),
    funcionarios: pick('employeesCount', 'employeeCount', 'staffCount', 'employeesOnLinkedIn', 'size'),
    site: pick('website'),
    sede: pick('headquarters', 'location'),
    decisores: lista(pick('dms', 'decisionMakers')).slice(0, 20).map(d => ({
      nome: d.name, headline: d.headline || '', linkedin_url: d.publicUrl || d.url || '',
    })),
    posts: lista(pick('posts')).slice(0, 5).map(p => ({
      texto: p.text || '', data: p.time || p.date || '', url: p.url || '',
    })),
    capturado_em: new Date().toISOString().slice(0, 19),
  },
};
"""


CANDIDATOS_JS = r"""// Resultado de Search People / Search Companies -> candidatos para a plataforma escolher.
// A plataforma só aceita um candidato se o nome e a empresa (ou o domínio do site) conferirem.
const alvo = $('Separar alvos').first().json;
const b = $json.body ?? $json;
let lista = b.data ?? b.result ?? b.results ?? b.items ?? b;
if (!Array.isArray(lista)) lista = lista.people ?? lista.companies ?? lista.results ?? lista.items ?? [];

return {
  json: {
    id_alvo: alvo.id_alvo,
    tipo: alvo.tipo,
    acao: 'buscar',
    candidatos: lista.slice(0, 10).map(c => ({
      nome: c.name || c.fullName || '',
      headline: c.headline || '',
      linkedin_url: c.publicUrl || c.url || c.profileUrl || c.companyUrl || '',
      local: c.location || '',
      site: c.website || '',
      setor: c.industry || '',
    })),
    capturado_em: new Date().toISOString().slice(0, 19),
  },
};
"""


def no(nome, tipo, versao, pos, params, **extra):
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/" + nome)), "name": nome, "type": tipo,
            "typeVersion": versao, "position": pos, "parameters": params, **extra}


def nota(nome, pos, texto, w=380, h=260, cor=4):
    return no(nome, "n8n-nodes-base.stickyNote", 1, pos, {"content": texto, "width": w, "height": h, "color": cor})


def atribuicao(nome, valor):
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/set/" + nome)), "name": nome, "value": valor, "type": "string"}


nodes = [
    no("Webhook", "n8n-nodes-base.webhook", 2, [0, 300], {
        "httpMethod": "POST", "path": "crosssell-linkedin", "authentication": "headerAuth",
        "responseMode": "onReceived", "options": {}},
        webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/webhook"))),
    no("Separar alvos", "n8n-nodes-base.splitOut", 1, [220, 300], {"fieldToSplitOut": "body.alvos", "options": {}}),
    no("Buscar?", "n8n-nodes-base.if", 2, [440, 300], {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
            "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/if-buscar")), "leftValue": "={{ $json.acao }}",
                            "rightValue": "buscar", "operator": {"type": "string", "operation": "equals"}}],
            "combinator": "and"},
        "options": {}}),
    no("Buscar pessoa?", "n8n-nodes-base.if", 2, [680, 760], {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
            "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/if-buscar-pessoa")), "leftValue": "={{ $json.tipo }}",
                            "rightValue": "pessoa", "operator": {"type": "string", "operation": "equals"}}],
            "combinator": "and"},
        "options": {}}),
    no("⚠ TROCAR: Linked API · Search People", "n8n-nodes-base.noOp", 1, [920, 680], {}),
    no("⚠ TROCAR: Linked API · Search Companies", "n8n-nodes-base.noOp", 1, [920, 860], {}),
    no("Aguardar busca pessoa", "n8n-nodes-base.wait", 1.1, [1140, 680], {
        "resume": "webhook", "httpMethod": "POST", "limitWaitTime": True, "limitType": "afterTimeInterval",
        "resumeAmount": 15, "resumeUnit": "minutes", "options": {}},
        webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/wait-busca-pessoa"))),
    no("Aguardar busca empresa", "n8n-nodes-base.wait", 1.1, [1140, 860], {
        "resume": "webhook", "httpMethod": "POST", "limitWaitTime": True, "limitType": "afterTimeInterval",
        "resumeAmount": 15, "resumeUnit": "minutes", "options": {}},
        webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/wait-busca-empresa"))),
    no("Candidatos", "n8n-nodes-base.code", 2, [1380, 760], {"mode": "runOnceForEachItem", "jsCode": CANDIDATOS_JS}),
    no("É pessoa?", "n8n-nodes-base.if", 2, [680, 300], {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
            "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/if")), "leftValue": "={{ $json.tipo }}",
                            "rightValue": "pessoa", "operator": {"type": "string", "operation": "equals"}}],
            "combinator": "and"},
        "options": {}}),
    no("⚠ TROCAR: Linked API · Fetch Person", "n8n-nodes-base.noOp", 1, [920, 200], {}),
    no("⚠ TROCAR: Linked API · Fetch Company", "n8n-nodes-base.noOp", 1, [920, 420], {}),
    no("Aguardar pessoa", "n8n-nodes-base.wait", 1.1, [1140, 200], {
        "resume": "webhook", "httpMethod": "POST", "limitWaitTime": True, "limitType": "afterTimeInterval",
        "resumeAmount": 15, "resumeUnit": "minutes", "options": {}},
        webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/wait-pessoa"))),
    no("Aguardar empresa", "n8n-nodes-base.wait", 1.1, [1140, 420], {
        "resume": "webhook", "httpMethod": "POST", "limitWaitTime": True, "limitType": "afterTimeInterval",
        "resumeAmount": 15, "resumeUnit": "minutes", "options": {}},
        webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/wait-empresa"))),
    no("Mapear", "n8n-nodes-base.code", 2, [1380, 300], {"mode": "runOnceForEachItem", "jsCode": MAPEAR_JS}),
    no("Registrar erro", "n8n-nodes-base.set", 3.4, [1380, 540], {
        "mode": "manual",
        "assignments": {"assignments": [
            atribuicao("id_alvo", "={{ $('Separar alvos').first().json.id_alvo }}"),
            atribuicao("tipo", "={{ $('Separar alvos').first().json.tipo }}"),
            atribuicao("erro", "={{ $json.error?.message || $json.error || 'Falha na Linked API' }}"),
        ]},
        "options": {}}),
    no("Devolver à plataforma", "n8n-nodes-base.httpRequest", 4.2, [1640, 420], {
        "method": "POST",
        "url": "={{ $('Webhook').first().json.body.callback_url }}",
        "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
        "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json) }}",
        "options": {"timeout": 30000}}, onError="continueRegularOutput"),
    nota("Leia antes", [-40, -60], (
        "## CrossSell · LinkedIn\n"
        "A plataforma chama este **Webhook** com um lote de alvos; cada alvo é consultado na Linked API "
        "e o resultado volta para a plataforma. O fluxo é linear (sem repetição): termina sozinho.\n\n"
        "**Credencial** (*Header Auth*): nome `Authorization`, valor `Bearer <N8N_TOKEN>`. "
        "Selecione-a no **Webhook** e no **Devolver à plataforma**.\n\n"
        "Ative o fluxo e copie a *Production URL* do Webhook para `N8N_LINKEDIN_WEBHOOK_URL` na plataforma."),
        w=620, h=300, cor=5),
    nota("Trocar nós da Linked API", [600, -160], (
        "## ⚠ Trocar os 2 nós marcados\n"
        "Substitua cada nó **⚠ TROCAR** pelo nó da Linked API (pacote `n8n-nodes-linked-api`):\n\n"
        "**Fetch Person**: Person URL `{{ $('Separar alvos').first().json.linkedin_url }}`; *Additional Data*: só Experience.\n\n"
        "**Fetch Company**: URL `{{ $json.linkedin_url }}` ou *Search Companies* com `{{ $json.nome }}`. "
        "Ative *decision makers* e *posts*.\n\n"
        "A Linked API responde de forma assíncrona: em **todos** os nós da Linked API, preencha "
        "*Webhook URL* com `{{ $execution.resumeUrl }}`. É esse endereço que acorda o nó **Aguardar** "
        "(Wait, *On Webhook Call*, limite de 15 min) com o resultado.\n\n"
        "Em ambos: *Settings → On Error → Continue (using error output)*, ligando a saída de erro "
        "ao nó **Registrar erro**."),
        w=520, h=420, cor=3),
]

conexoes = {
    "Webhook": [["Separar alvos"]],
    "Separar alvos": [["Buscar?"]],
    "Buscar?": [["Buscar pessoa?"], ["É pessoa?"]],
    "Buscar pessoa?": [["⚠ TROCAR: Linked API · Search People"], ["⚠ TROCAR: Linked API · Search Companies"]],
    "⚠ TROCAR: Linked API · Search People": [["Aguardar busca pessoa"]],
    "⚠ TROCAR: Linked API · Search Companies": [["Aguardar busca empresa"]],
    "Aguardar busca pessoa": [["Candidatos"]],
    "Aguardar busca empresa": [["Candidatos"]],
    "Candidatos": [["Devolver à plataforma"]],
    "É pessoa?": [["⚠ TROCAR: Linked API · Fetch Person"], ["⚠ TROCAR: Linked API · Fetch Company"]],
    "⚠ TROCAR: Linked API · Fetch Person": [["Aguardar pessoa"]],
    "⚠ TROCAR: Linked API · Fetch Company": [["Aguardar empresa"]],
    "Aguardar pessoa": [["Mapear"]],
    "Aguardar empresa": [["Mapear"]],
    "Mapear": [["Devolver à plataforma"]],
    "Registrar erro": [["Devolver à plataforma"]],
}

fluxo = {
    "name": "CrossSell · LinkedIn (Linked API)",
    "nodes": nodes,
    "connections": {
        origem: {"main": [[{"node": d, "type": "main", "index": 0} for d in saida] for saida in saidas]}
        for origem, saidas in conexoes.items()
    },
    "settings": {"executionOrder": "v1"},
    "pinData": {},
    "active": False,
}

saida = Path(__file__).with_name("crosssell-linkedin.json")
saida.write_text(json.dumps(fluxo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(saida)
