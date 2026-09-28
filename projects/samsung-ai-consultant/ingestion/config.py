"""Configuration loading for the Samsung ingestion pipeline.

Reads DATABASE_URL and source/crawl settings from the environment
(optionally populated from a .env file), following the same pattern as
tools/n8n-tool/n8n_tool/config.py. Never logs or prints DATABASE_URL,
since it embeds credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import os

from dotenv import load_dotenv

DEFAULT_CATALOG_URL = "https://galaxystore.ru/catalog/televizory/year=2026/"
DEFAULT_SOURCE_NAME = "galaxystore"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "SamsungIngestionBot/0.1 (+contact: ruslan.babashaikhov@gmail.com)"
)
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_DELAY_SECONDS = 2.0


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    database_url: str
    source: str = DEFAULT_SOURCE_NAME
    catalog_url: str = DEFAULT_CATALOG_URL
    user_agent: str = DEFAULT_USER_AGENT
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    delay_seconds: float = DEFAULT_DELAY_SECONDS

    @property
    def masked_database_url(self) -> str:
        """A safe-to-print representation of DATABASE_URL, never the full value."""
        if "@" not in self.database_url:
            return "***"
        _, _, tail = self.database_url.partition("@")
        return f"***@{tail}"


def load_config(env_file: Path | str | None = None, *, require_database: bool = True) -> Config:
    """Load configuration from the environment / a .env file.

    Existing environment variables always take precedence over values in
    the .env file, matching standard dotenv semantics. `require_database`
    is set to False for commands (e.g. `fetch-product`) that never touch
    PostgreSQL.
    """
    dotenv_path = Path(env_file) if env_file is not None else Path(".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if require_database and not database_url:
        raise ConfigError("Missing required environment variable: DATABASE_URL")

    catalog_url = os.environ.get("SAMSUNG_CATALOG_URL", "").strip() or DEFAULT_CATALOG_URL
    source = os.environ.get("SAMSUNG_SOURCE", "").strip() or DEFAULT_SOURCE_NAME

    def _float_env(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default

    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        return int(raw) if raw else default

    return Config(
        database_url=database_url,
        source=source,
        catalog_url=catalog_url,
        user_agent=os.environ.get("SAMSUNG_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        request_timeout_seconds=_float_env(
            "SAMSUNG_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
        max_retries=_int_env("SAMSUNG_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        backoff_base_seconds=_float_env(
            "SAMSUNG_BACKOFF_BASE_SECONDS", DEFAULT_BACKOFF_BASE_SECONDS
        ),
        delay_seconds=_float_env("SAMSUNG_DELAY_SECONDS", DEFAULT_DELAY_SECONDS),
    )
