"""Configuration loading for the RAG document/chunk indexing pipeline.

Deliberately separate from `ingestion/config.py`: indexing reads
`INDEXING_DATABASE_URL`, not `DATABASE_URL`, and connects as its own
least-privilege Postgres role (`samsung_indexing`) scoped to
`documents`/`chunks` (+ read-only `products`/`product_specs`) -- never the
`samsung_ingestion` role's own credential. See
projects/ai-catalog-consultant/README.md "Database access" for how both
roles were created.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import os

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    database_url: str

    @property
    def masked_database_url(self) -> str:
        if "@" not in self.database_url:
            return "***"
        _, _, tail = self.database_url.partition("@")
        return f"***@{tail}"


def load_config(env_file: Path | str | None = None, *, require_database: bool = True) -> Config:
    dotenv_path = Path(env_file) if env_file is not None else Path(".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)

    database_url = os.environ.get("INDEXING_DATABASE_URL", "").strip()
    if require_database and not database_url:
        raise ConfigError("Missing required environment variable: INDEXING_DATABASE_URL")

    return Config(database_url=database_url)
