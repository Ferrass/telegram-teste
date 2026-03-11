from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = Field(..., env="DATABASE_URL")
    secret_key: str = Field(..., env="SECRET_KEY")
    encryption_key: str = Field(..., env="ENCRYPTION_KEY")
    access_token_expire_minutes: int = 60 * 24 * 7
    telegram_api_id: int = Field(..., env="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(..., env="TELEGRAM_API_HASH")
    app_env: str = Field("development", env="APP_ENV")
    log_level: str = Field("INFO", env="LOG_LEVEL")
    scheduler_interval_seconds: int = Field(60, env="SCHEDULER_INTERVAL_SECONDS")
    metrics_interval_seconds: int = Field(1800, env="METRICS_INTERVAL_SECONDS")

    # Cloudflare R2
    r2_endpoint: str = Field("", env="R2_ENDPOINT")
    r2_access_key: str = Field("", env="R2_ACCESS_KEY")
    r2_secret_key: str = Field("", env="R2_SECRET_KEY")
    r2_bucket: str = Field("", env="R2_BUCKET")
    r2_public_url: str = Field("", env="R2_PUBLIC_URL")

    model_config = {"env_file": ".env", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()