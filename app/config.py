"""
Application Configuration and Settings Module.

Loads configuration from environment variables or .env file with robust defaults.
Seamlessly supports pydantic-settings when installed, with a built-in fallback.
"""

import os
from functools import lru_cache
from typing import List, Optional

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _HAS_PYDANTIC_SETTINGS = True
except ImportError:
    from pydantic import BaseModel as BaseSettings
    SettingsConfigDict = None
    _HAS_PYDANTIC_SETTINGS = False


def _parse_list_from_env(key: str, default: List[str]) -> List[str]:
    """Helper to parse comma-separated or json lists from env vars."""
    val = os.getenv(key)
    if not val:
        return default
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        import json
        try:
            return json.loads(val)
        except Exception:
            pass
    return [item.strip() for item in val.split(",") if item.strip()]


class Settings(BaseSettings):
    """
    Global application configuration settings.
    """
    if _HAS_PYDANTIC_SETTINGS:
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

    # Application Information
    APP_NAME: str = "AI-Powered Website Link Analysis API"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 4000
    ALLOWED_ORIGINS: List[str] = ["*"]

    # LLM Provider Configuration ("gemini", "openai", or "mock")
    LLM_PROVIDER: str = "gemini"

    # Google Gemini Configuration
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-2.0-flash"

    # OpenAI Configuration
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Web Scraping & Content Limits
    TIMEOUT_SECONDS: int = 15
    MAX_CONTENT_LENGTH: int = 5242880  # 5 MB maximum HTML payload
    MAX_LLM_INPUT_CHARS: int = 15000   # Max characters sent to LLM prompt
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 (compatible; LinkAnalysisBot/1.0)"
    )

    # Security & SSRF
    CHECK_DNS_SSRF: bool = True

    def __init__(self, **kwargs):
        # If pydantic-settings is not available, populate from os.environ
        if not _HAS_PYDANTIC_SETTINGS:
            _env_file = ".env"
            if os.path.exists(_env_file):
                try:
                    with open(_env_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                k = k.strip()
                                v = v.strip().strip("'\"")
                                if k not in os.environ:
                                    os.environ[k] = v
                except Exception:
                    pass

            # Read env overrides
            env_values = {
                "APP_NAME": os.getenv("APP_NAME", "AI-Powered Website Link Analysis API"),
                "APP_VERSION": os.getenv("APP_VERSION", "1.0.0"),
                "ENVIRONMENT": os.getenv("ENVIRONMENT", "development"),
                "DEBUG": os.getenv("DEBUG", "true").lower() in ("true", "1", "yes"),
                "HOST": os.getenv("HOST", "0.0.0.0"),
                "PORT": int(os.getenv("PORT", "4000")),
                "ALLOWED_ORIGINS": _parse_list_from_env("ALLOWED_ORIGINS", ["*"]),
                "LLM_PROVIDER": os.getenv("LLM_PROVIDER", "gemini"),
                "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
                "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
                "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
                "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                "TIMEOUT_SECONDS": int(os.getenv("TIMEOUT_SECONDS", "15")),
                "MAX_CONTENT_LENGTH": int(os.getenv("MAX_CONTENT_LENGTH", "5242880")),
                "MAX_LLM_INPUT_CHARS": int(os.getenv("MAX_LLM_INPUT_CHARS", "15000")),
                "USER_AGENT": os.getenv(
                    "USER_AGENT",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36 (compatible; LinkAnalysisBot/1.0)",
                ),
                "CHECK_DNS_SSRF": os.getenv("CHECK_DNS_SSRF", "true").lower() in ("true", "1", "yes"),
            }
            env_values.update(kwargs)
            super().__init__(**env_values)
        else:
            super().__init__(**kwargs)


@lru_cache()
def get_settings() -> Settings:
    """Returns cached application settings instance."""
    return Settings()


# Singleton instance for quick access
settings = get_settings()
