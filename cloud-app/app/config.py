from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./zoho_tally.db"
    cloud_api_key: str = "dev-cloud-api-key"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
