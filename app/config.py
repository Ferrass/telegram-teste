"""
Application configuration — carregado de variáveis de ambiente.
Nunca armazene segredos no código.
"""
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────
    database_url: str = Field(..., env="DATABASE_URL")

    # ── Security ──────────────────────────────────────────────────
    secret_key: str = Field(..., env="SECRET_KEY")
    encryption_key: str = Field(..., env="ENCRYPTION_KEY")
    access_token_expire_minutes: int = 30          # token curto
    refresh_token_expire_days: int = 7

    # ── Telegram ──────────────────────────────────────────────────
    telegram_api_id: int = Field(..., env="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(..., env="TELEGRAM_API_HASH")

    # ── App ───────────────────────────────────────────────────────
    app_env: str = Field("development", env="APP_ENV")
    log_level: str = Field("INFO", env="LOG_LEVEL")
    allowed_origins: str = Field("*", env="ALLOWED_ORIGINS")  # vírgula separada

    # ── Worker ────────────────────────────────────────────────────
    scheduler_interval_seconds: int = Field(60, env="SCHEDULER_INTERVAL_SECONDS")
    metrics_interval_seconds: int = Field(1800, env="METRICS_INTERVAL_SECONDS")

    # ── Cloudflare R2 ─────────────────────────────────────────────
    r2_endpoint: str = Field("", env="R2_ENDPOINT")
    r2_access_key: str = Field("", env="R2_ACCESS_KEY")
    r2_secret_key: str = Field("", env="R2_SECRET_KEY")
    r2_bucket: str = Field("", env="R2_BUCKET")
    r2_public_url: str = Field("", env="R2_PUBLIC_URL")

    # ── Rate Limiting ─────────────────────────────────────────────
    rate_limit_login: str = "5/minute"
    rate_limit_send_code: str = "3/minute"
    rate_limit_register: str = "3/minute"
    rate_limit_schedule: str = "30/minute"

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    model_config = {"env_file": ".env", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
