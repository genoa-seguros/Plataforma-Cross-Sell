"""Gera n8n/crosssell-linkedin.json (fluxo importável no n8n). Rode: python n8n/gerar_fluxo.py"""

import json
import uuid
from pathlib import Path

MAPEAR_JS = r"""// Converte a resposta da Linked API no formato que a plataforma espera.
// Os nomes dos campos variam entre versões: o pick() tenta alternativas.
// Na primeira execução, confira a saída dos nós da Linked API e ajuste se algum campo vier vazio.
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
    no("Loop Over Items", "n8n-nodes-base.splitInBatches", 3, [440, 300], {"batchSize": 1, "options": {}}),
    no("Pausa", "n8n-nodes-base.wait", 1.1, [660, 380], {"resume": "timeInterval", "amount": 30, "unit": "seconds"},
       webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/wait"))),
    no("É pessoa?", "n8n-nodes-base.if", 2, [880, 380], {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
            "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "crosssell/if")), "leftValue": "={{ $json.tipo }}",
                            "rightValue": "pessoa", "operator": {"type": "string", "operation": "equals"}}],
            "combinator": "and"},
        "options": {}}),
    no("⚠ TROCAR: Linked API · Fetch Person", "n8n-nodes-base.noOp", 1, [1120, 280], {}),
    no("⚠ TROCAR: Linked API · Fetch Company", "n8n-nodes-base.noOp", 1, [1120, 480], {}),
    no("Mapear", "n8n-nodes-base.code", 2, [1360, 380], {"mode": "runOnceForEachItem", "jsCode": MAPEAR_JS}),
    no("Registrar erro", "n8n-nodes-base.set", 3.4, [1360, 640], {
        "mode": "manual",
        "assignments": {"assignments": [
            atribuicao("id_alvo", "={{ $('Loop Over Items').item.json.id_alvo }}"),
            atribuicao("tipo", "={{ $('Loop Over Items').item.json.tipo }}"),
            atribuicao("erro", "={{ $json.error?.message || $json.error || 'Falha na Linked API' }}"),
        ]},
        "options": {}}),
    no("Devolver à plataforma", "n8n-nodes-base.httpRequest", 4.2, [1600, 380], {
        "method": "POST",
        "url": "={{ $('Webhook').first().json.body.callback_url }}",
        "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
        "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json) }}",
        "options": {"timeout": 30000}}),
    nota("Leia antes", [-40, -60], (
        "## CrossSell · LinkedIn\n"
        "A plataforma chama este **Webhook** com um lote de alvos; cada alvo é consultado na Linked API "
        "e o resultado volta para a plataforma.\n\n"
        "**Credencial** (*Header Auth*): nome `Authorization`, valor `Bearer <N8N_TOKEN>`. "
        "Selecione-a no **Webhook** e no **Devolver à plataforma**.\n\n"
        "Ative o fluxo e copie a *Production URL* do Webhook para `N8N_LINKEDIN_WEBHOOK_URL` na plataforma."),
        w=620, h=300, cor=5),
    nota("Trocar nós da Linked API", [1040, 60], (
        "## ⚠ Trocar os 2 nós marcados\n"
        "Substitua cada nó **⚠ TROCAR** pelo nó da Linked API (pacote `n8n-nodes-linked-api`):\n\n"
        "**Fetch Person**: URL `{{ $json.linkedin_url }}`. Se vazia, use antes *Search People* com "
        "`{{ $json.nome }}` + `{{ $json.empresa }}` e pegue o 1º resultado. Ative *experience*.\n\n"
        "**Fetch Company**: URL `{{ $json.linkedin_url }}` ou *Search Companies* com `{{ $json.nome }}`. "
        "Ative *decision makers* e *posts*.\n\n"
        "Em ambos: *Settings → On Error → Continue (using error output)*, ligando a saída de erro "
        "ao nó **Registrar erro**."),
        w=520, h=420, cor=3),
]

conexoes = {
    "Webhook": [["Separar alvos"]],
    "Separar alvos": [["Loop Over Items"]],
    "Loop Over Items": [[], ["Pausa"]],          # saída 0 = concluído, saída 1 = próximo item
    "Pausa": [["É pessoa?"]],
    "É pessoa?": [["⚠ TROCAR: Linked API · Fetch Person"], ["⚠ TROCAR: Linked API · Fetch Company"]],
    "⚠ TROCAR: Linked API · Fetch Person": [["Mapear"]],
    "⚠ TROCAR: Linked API · Fetch Company": [["Mapear"]],
    "Mapear": [["Devolver à plataforma"]],
    "Registrar erro": [["Devolver à plataforma"]],
    "Devolver à plataforma": [["Loop Over Items"]],
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
