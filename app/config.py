from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:////data/gas_prices.db"
    google_client_id: str = ""
    google_client_secret: str = ""
    session_secret: str = "change-me-in-production"
    base_url: str = "http://localhost:8080"

    model_config = {"env_file": ".env"}


settings = Settings()
