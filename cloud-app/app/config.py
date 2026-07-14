from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./zoho_tally.db"
    cloud_api_key: str = "dev-cloud-api-key"

    # Zoho Books India Edition
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_redirect_uri: str = "http://localhost:8000/oauth/zoho/callback"
    zoho_api_domain: str = "https://www.zohoapis.in"
    zoho_books_org_id: str = ""

    # Tally Agent
    tally_agent_base_url: str = "http://agent:8010"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
