"""Pipedrive falso (httpx.MockTransport) com dados mínimos e registro das escritas."""

import json
from datetime import date, timedelta

import httpx

HOJE = date.today()
CNPJ_FIELD = "4f808fee58c9a509b20237a2ffb8b3f169293b0f"
INI, FIM = "3ee3bdd07bab71fba84767ffb7d5d89f49b1f3d3", "0d4a74f324a5c95618a51042c3185da9c8846bc3"
PROD_LF = "243db2a0f48ee484d65af0c737bcea6ab509a590"
PROD_RE = "87667ea5ea62cfd03118a42581f40bc5fb33e973"
POSSUI_SAUDE = "dced275b9f49ad3ac158c4a961bc17920ad4de57"


def d(dias: int) -> str:
    return (HOJE + timedelta(days=dias)).isoformat()


def deal(id, pipeline, status, org, person=None, title="", owner=1, **campos):
    return {"id": id, "title": title, "pipeline_id": pipeline, "stage_id": 4, "status": status, "value": 1000,
            "org_id": org, "person_id": person, "owner_id": owner, "custom_fields": campos}


DADOS = {
    "users": [{"id": 1, "email": "victor.boldrini@innoaseguros.com.br"},
              {"id": 2, "email": "bruno.rodrigues@innoaseguros.com.br"}],
    "stages": [{"id": 4, "name": "Em Cotação"}],
    "organizations": [
        {"id": 10, "name": "Metalúrgica Alfa Ltda", "custom_fields": {CNPJ_FIELD: "14.069.185/0001-03"},
         "website": "https://alfa.com.br"},
        {"id": 20, "name": "Beta Serviços SA", "custom_fields": {CNPJ_FIELD: "11.444.777/0001-61"}},
    ],
    "persons": [
        {"id": 100, "name": "Ana Souza", "org_id": 10, "job_title": "CFO", "emails": [{"value": "ana@alfa.com.br"}]},
        {"id": 200, "name": "Caio Lima", "org_id": 20, "job_title": "Gerente", "emails": [{"value": "caio@beta.com.br"}]},
    ],
    "deals": [
        # Alfa: cliente de LF com D&O vigente, Cyber vencido e um ganho sem fim de vigência
        deal(1, 1, "won", 10, 100, "D&O 2026", **{PROD_LF: {"id": 28, "label": "D&O"}, INI: d(-300), FIM: d(65)}),
        deal(2, 1, "won", 10, 100, "Cyber 2025", **{PROD_LF: {"id": 30, "label": "Cyber"}, INI: d(-500), FIM: d(-135)}),
        deal(3, 40, "won", 10, None, "Garantia 2026", **{INI: d(-10)}),
        # Alfa: negócio aberto de RE (entra na tabela)
        deal(4, 29, "open", 10, 100, "Empresarial 2026", owner=2, **{PROD_RE: {"id": 1, "label": "Empresarial"}}),
        # Beta: lead com negócio aberto em Saúde e "já possui saúde" = Sim em outro negócio
        deal(5, 23, "open", 20, 200, "Saúde 2026", owner=1),
        deal(6, 1, "lost", 20, 200, "D&O 2025", **{POSSUI_SAUDE: {"id": 452, "label": "Sim"}}),
        # Fora da tabela: Garantia (40), Flash (38), M&A (31, nem configurado)
        deal(7, 40, "open", 20, 200, "Garantia nova"),
        deal(8, 38, "open", 20, 200, "Lead Flash"),
        deal(9, 31, "open", 20, None, "M&A"),
        # Ganho "cancelamento" não conta como vigente
        deal(10, 1, "won", 20, 200, "Medmal 2026 Cancelamento", **{INI: d(-30), FIM: d(330)}),
    ],
}


class FakePipedrive:
    def __init__(self):
        self.criadas: list[dict] = []
        self.atualizadas: list[tuple[int, dict]] = []
        self.feitas: set[int] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def _handler(self, req: httpx.Request) -> httpx.Response:
        caminho = req.url.path
        if req.method == "POST" and caminho.endswith("/v2/activities"):
            corpo = json.loads(req.content)
            corpo["id"] = 900 + len(self.criadas)
            self.criadas.append(corpo)
            return httpx.Response(201, json={"data": corpo})
        if req.method == "PATCH" and "/v2/activities/" in caminho:
            aid = int(caminho.rsplit("/", 1)[1])
            corpo = json.loads(req.content)
            self.atualizadas.append((aid, corpo))
            (self.feitas.add if corpo.get("done") else self.feitas.discard)(aid)
            return httpx.Response(200, json={"data": {"id": aid, **corpo}})
        if req.method == "GET" and "/v2/activities/" in caminho:
            aid = int(caminho.rsplit("/", 1)[1])
            return httpx.Response(200, json={"data": {"id": aid, "done": aid in self.feitas}})
        recurso = caminho.rsplit("/", 1)[1]
        if recurso == "users":
            return httpx.Response(200, json={"data": DADOS["users"]})
        return httpx.Response(200, json={"data": DADOS[recurso], "additional_data": {"next_cursor": None}})
