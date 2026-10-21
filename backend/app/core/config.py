from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    POSTGRES_USER: str = "acac_admin"
    POSTGRES_PASSWORD: str = "acac_dev_password_2026"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "acac_db"
    DATABASE_URL: Optional[str] = None

    SECRET_KEY: str = "super-secret-acac-jwt-key-change-in-production"
    JWT_SECRET_KEY: Optional[str] = None
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720
    UPLOAD_DIR: str = "/app/uploads"

    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_SECONDS: int = 900

    ESSAY_MAX_PAGES: int = 5
    ESSAY_MIN_WORDS: int = 1000

    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    CORS_ORIGINS: Optional[str] = None

    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_API_URL: str = "https://api.deepseek.com/v1/chat/completions"
    DEEPSEEK_MODEL: str = "deepseek-v4-pro"
    GEMMA_API_KEY: str = ""
    GEMMA_API_URL: str = "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-26b-a4b-it:generateContent"
    GEMMA_MODEL: str = "gemma-4-26b-a4b-it"
    AI_MODE: str = "stub"  # "stub" (offline/tests) or "real" (live APIs)

    @property
    def async_database_url(self) -> str:
        if self.DATABASE_URL:
            if self.DATABASE_URL.startswith("postgresql://"):
                return self.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
            return self.DATABASE_URL
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def cors_origins(self) -> list[str]:
        raw = self.CORS_ORIGINS or self.ALLOWED_ORIGINS or ""
        if isinstance(raw, list):
            return raw
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
