from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

VERTICAIS = ("linhas_financeiras", "saude", "ramos_elementares")
VERTICAL_LABEL = {
    "linhas_financeiras": "Linhas Financeiras",
    "saude": "Saúde",
    "ramos_elementares": "Ramos Elementares",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./crosssell.db"
    verticais_file: Path = Path("config/verticais.yaml")

    pipedrive_api_token: str = ""
    pipedrive_company_domain: str = "api"
    pipedrive_cnpj_field: str = "4f808fee58c9a509b20237a2ffb8b3f169293b0f"

    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""

    internal_domains: str = "innoaseguros.com.br"

    # Temperatura dos e-mails (Claude API). A chave vem de ANTHROPIC_API_KEY.
    anthropic_model: str = "claude-opus-5-5"
    temperatura_max_emails: int = 5

    # LinkedIn via Google Sheets + n8n (Linked API). A planilha é compartilhada com a conta de serviço.
    google_sheets_id: str = ""
    google_service_account_file: str = ""
    linkedin_aba_alvos: str = "Alvos"
    linkedin_aba_resultados: str = "Resultados"
    linkedin_validade_dias: int = 90  # reler perfis com mais de N dias

    # Login: o master é criado pelo comando `crosssell criar-master`.
    sessao_dias: int = 14
    cookie_seguro: bool = True  # exige HTTPS em produção; desligue só em ambiente local

    @property
    def internal_domain_set(self) -> set[str]:
        return {d.strip().lower() for d in self.internal_domains.split(",") if d.strip()}

    def verticais_config(self) -> dict:
        if not self.verticais_file.exists():
            return {}
        return yaml.safe_load(self.verticais_file.read_text(encoding="utf-8")) or {}

    def pipelines(self) -> dict[int, dict]:
        """pipeline_id -> {nome, vertical, tabela}."""
        brutos = (self.verticais_config().get("pipedrive") or {}).get("pipelines") or {}
        return {int(k): {"nome": v.get("nome") or f"Funil {k}", "vertical": v.get("vertical"),
                         "tabela": bool(v.get("tabela"))} for k, v in brutos.items()}

    def campos_pipedrive(self) -> dict:
        return (self.verticais_config().get("pipedrive") or {}).get("campos") or {}

    def usuarios_iniciais(self) -> list[dict]:
        """Equipe inicial do config: email, nome, verticais, lider."""
        saida = []
        for u in self.verticais_config().get("usuarios") or []:
            saida.append({
                "email": u["email"].strip().lower(),
                "nome": u.get("nome") or u["email"].split("@")[0],
                "verticais": list(u.get("verticais") or []),
                "lider": list(u.get("lider") or []),
            })
        return saida


@lru_cache
def get_settings() -> Settings:
    return Settings()
