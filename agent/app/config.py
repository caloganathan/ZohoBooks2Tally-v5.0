from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    cloud_base_url: str = "http://cloud-app:8000"
    cloud_api_key: str = "dev-cloud-api-key"
    agent_name: str = "local-agent-1"
    agent_poll_size: int = 10
    agent_local_store: str = "/data/agent.json"
    connector_id: str | None = None
    connector_secret: str | None = None
    
    # Tally HTTP endpoint (TDL XML/HTTP)
    tally_base_url: str = "http://host.docker.internal:9000"
    tally_company: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()