import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]


def _load_dotenv() -> dict[str, str]:
    env_path = ROOT_DIR / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/shocks_art.db"
    youtube_api_key: str = ""
    youtube_channel_handle: str = "shocksart"
    youtube_oauth_client_secrets_file: str = ""
    youtube_oauth_redirect_uri: str = "http://127.0.0.1:8000/analytics/oauth2callback"
    youtube_token_encryption_key: str = ""
    youtube_analytics_backfill_start: str = "2026-06-01"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    prompt_version: str = "1.0"
    schema_version: str = "1.0"
    max_retries: int = 2
    processing_concurrency: int = 1
    candidates_per_stream: int = 3


@lru_cache
def get_settings() -> Settings:
    dotenv = _load_dotenv()

    def read(name: str, default: str) -> str:
        return os.getenv(name, dotenv.get(name, default))

    return Settings(
        database_url=read("DATABASE_URL", Settings.database_url),
        youtube_api_key=read("YOUTUBE_API_KEY", ""),
        youtube_channel_handle=read("YOUTUBE_CHANNEL_HANDLE", Settings.youtube_channel_handle),
        youtube_oauth_client_secrets_file=read("YOUTUBE_OAUTH_CLIENT_SECRETS_FILE", ""),
        youtube_oauth_redirect_uri=read(
            "YOUTUBE_OAUTH_REDIRECT_URI",
            Settings.youtube_oauth_redirect_uri,
        ),
        youtube_token_encryption_key=read("YOUTUBE_TOKEN_ENCRYPTION_KEY", ""),
        youtube_analytics_backfill_start=read(
            "YOUTUBE_ANALYTICS_BACKFILL_START",
            Settings.youtube_analytics_backfill_start,
        ),
        gemini_api_key=read("GEMINI_API_KEY", ""),
        gemini_model=read("GEMINI_MODEL", Settings.gemini_model),
        prompt_version=read("PROMPT_VERSION", Settings.prompt_version),
        schema_version=read("SCHEMA_VERSION", Settings.schema_version),
        max_retries=int(read("MAX_RETRIES", str(Settings.max_retries))),
        processing_concurrency=int(read("PROCESSING_CONCURRENCY", str(Settings.processing_concurrency))),
        candidates_per_stream=int(read("CANDIDATES_PER_STREAM", str(Settings.candidates_per_stream))),
    )
