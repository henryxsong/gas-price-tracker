from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:////data/gas_prices.db"

    model_config = {"env_file": ".env"}


settings = Settings()
