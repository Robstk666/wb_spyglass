"""
config.py — Централизованные настройки и зависимости FastAPI.

Загружает переменные окружения через pydantic-settings,
предоставляет FastAPI-зависимость для проверки X-API-Key.
"""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from pydantic_settings import BaseSettings, SettingsConfigDict


# ─────────────────────────────────────────────────────────────────────────────
# 1. Модель настроек (читает .env автоматически)
# ─────────────────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    """Все переменные окружения проекта."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",          # игнорировать неизвестные переменные в .env
    )

    # Безопасность
    secret_api_key: str = "changeme"

    # OpenRouter AI
    openrouter_api_key: str = ""

    # CORS — список строк через запятую; парсим в свойстве
    allowed_origins: str = "http://localhost:3000,http://localhost:5173"

    # Окружение
    app_env: str = "development"

    # Telegram
    telegram_bot_token: str = ""
    admin_chat_id: str = ""

    # Proxy для WB API (опционально)
    proxy_url: str = ""
    scraper_api_key: str = ""

    # ── Вспомогательные свойства ──────────────────────────────────────────
    @property
    def cors_origins(self) -> list[str]:
        """Возвращает список разрешённых origin для CORS."""
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


# Singleton через lru_cache — инициализируется один раз при старте
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ─────────────────────────────────────────────────────────────────────────────
# 2. FastAPI Dependency — проверка X-API-Key
# ─────────────────────────────────────────────────────────────────────────────

async def verify_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """
    Зависимость FastAPI: проверяет наличие и корректность заголовка X-API-Key.

    Использование в роутере:
        @router.get("/protected", dependencies=[Depends(verify_api_key)])

    Raises:
        HTTPException 401 — если ключ отсутствует или не совпадает.
    """
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if x_api_key != settings.secret_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
