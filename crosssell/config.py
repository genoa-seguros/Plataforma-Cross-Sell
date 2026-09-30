from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

VERTICAIS = ("linhas_financeiras", "saude", "ramos_elementares", "linhas_pessoais")
VERTICAL_LABEL = {
    "linhas_financeiras": "Linhas Financeiras",
    "saude": "Saúde",
    "ramos_elementares": "Ramos Elementares",
    "linhas_pessoais": "Linhas Pessoais",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./crosssell.db"
    verticais_file: Path = Path("config/verticais.yaml")

    pipedrive_api_token: str = ""
    pipedrive_company_domain: str = "api"
    pipedrive_cnpj_field: str = "4f808fee58c9a509b20237a2ffb8b3f169293b0f"
    # Campos customizados de negócio com a vigência da apólice.
    pipedrive_inicio_vigencia_field: str = "3ee3bdd07bab71fba84767ffb7d5d89f49b1f3d3"
    pipedrive_fim_vigencia_field: str = "0d4a74f324a5c95618a51042c3185da9c8846bc3"

    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""

    internal_domains: str = "innoaseguros.com.br"

    @property
    def internal_domain_set(self) -> set[str]:
        return {d.strip().lower() for d in self.internal_domains.split(",") if d.strip()}

    def verticais_config(self) -> dict:
        if not self.verticais_file.exists():
            return {}
        return yaml.safe_load(self.verticais_file.read_text(encoding="utf-8")) or {}

    def usuarios(self) -> list[dict]:
        """Usuários internos normalizados: email, nome, verticais, lider."""
        saida = []
        for u in self.verticais_config().get("usuarios") or []:
            verticais = u.get("verticais") or ([u["vertical"]] if u.get("vertical") else [])
            saida.append({
                "email": u["email"].strip().lower(),
                "nome": u.get("nome") or u["email"].split("@")[0],
                "verticais": list(verticais),
                "lider": list(u.get("lider") or []),
            })
        return saida

    def responsaveis(self, vertical: str) -> list[str]:
        """Quem recebe oportunidades da vertical: o(s) líder(es) ou, sem líder, toda a equipe."""
        equipe = [u for u in self.usuarios() if vertical in u["verticais"]]
        lideres = [u["email"] for u in equipe if vertical in u["lider"]]
        return lideres or [u["email"] for u in equipe]


@lru_cache
def get_settings() -> Settings:
    return Settings()
